import sublime
import sublime_plugin

import htmlmin
import json
import os
from jinja2 import Template
from os.path import realpath
from threading import Lock


from ..lib import deferred, link_opener, logger, settings, requests


__all__ = [
    'EditorEventListener',
    'EditorCompletionsListener',
    'EditorSignaturesListener',
]


_DEBUG = os.getenv('SUBLIME_DEV')

def _is_view_supported(view):
    return view.file_name() is not None and view.file_name().endswith('.py')

def _check_view_size(view):
    return view.size() <= (1 << 20)

def _in_function_call(view, point):
    return (view.match_selector(point, 'meta.function-call.python') and
            not view.match_selector(point, 'variable.function.python'))


class EditorEventListener(sublime_plugin.EventListener):
    """Listener which forwards editor events to the event endpoint and also
    fetches completions and function signature information when the proper
    event triggers are fired.
    """

    _last_selection_region = None

    def on_modified(self, view):
        self._handle(view, 'edit')

    def on_selection_modified(self, view):
        self._handle(view, 'selection')

    @classmethod
    def _handle(cls, view, action):
        if not _is_view_supported(view):
            return

        deferred.defer(requests.kited_post, '/clientapi/editor/event',
                       data=cls._event_data(view, action))

        if action == 'selection':
            select_region = cls._view_region(view)
            cls._last_selection_region = select_region

            if _in_function_call(view, select_region['end']):
                if EditorSignaturesListener.is_activated():
                    EditorSignaturesListener.queue_signatures(
                        view, select_region['end'])
            else:
                EditorSignaturesListener.hide_signatures(view)

        if action == 'edit' and _check_view_size(view):
            edit_region = cls._view_region(view)
            edit_type, num_chars = cls._edit_info(cls._last_selection_region,
                                                  edit_region)

            if edit_type == 'insertion' and num_chars == 1:
                EditorCompletionsListener.queue_completions(view,
                                                            edit_region['end'])
            elif edit_type == 'deletion' and num_chars > 1:
                EditorCompletionsListener.hide_completions(view)

            if _in_function_call(view, edit_region['end']):
                EditorSignaturesListener.queue_signatures(view,
                                                          edit_region['end'])
            else:
                EditorSignaturesListener.hide_signatures(view)

    @staticmethod
    def _view_region(view):
        if len(view.sel()) != 1:
            return None

        r = view.sel()[0]
        return {
            'file': view.file_name(),
            'begin': r.begin(),
            'end': r.end(),
        }

    @staticmethod
    def _edit_info(selection, edit):
        no_info = (None, None)

        if (selection is None or edit is None or
            selection['file'] != edit['file']):
            return no_info

        if (edit['end'] > selection['end']):
            return ('insertion', edit['end'] - selection['end'])
        if (edit['end'] < selection['end']):
            return ('deletion', selection['end'] - edit['end'])

        return no_info

    @staticmethod
    def _event_data(view, action):
        text = view.substr(sublime.Region(0, view.size()))

        if not _check_view_size(view):
            action = 'skip'
            text = ''

        return {
            'source': 'sublime3',
            'filename': realpath(view.file_name()),
            'text': text,
            'action': action,
            'selections': [{'start': r.a, 'end': r.b} for r in view.sel()],
        }


class EditorCompletionsListener(sublime_plugin.EventListener):
    """Listener which handles completions by preemptively forwarding requests
    to the completions endpoint and then running the Sublime `auto_complete`
    command.
    """

    _received_completions = []
    _lock = Lock()

    def on_query_completions(self, view, prefix, locations):
        cls = self.__class__

        if not _is_view_supported(view):
            return None

        if not _check_view_size(view):
            return None

        if len(locations) != 1:
            return None

        with cls._lock:
            completions = [
                (self._brand_completion(c['display'], c['hint']),
                 c['insert']) for c in cls._received_completions
            ]
            cls._received_completions = []
            return completions

    @classmethod
    def queue_completions(cls, view, location):
        deferred.defer(cls._request_completions,
                       view, cls._event_data(view, location))

    @classmethod
    def hide_completions(cls, view):
        with cls._lock:
            cls._received_completions = []
        view.run_command('hide_auto_complete')

    @classmethod
    def _request_completions(cls, view, data):
        resp, body = requests.kited_post('/clientapi/editor/completions', data)

        if resp.status != 200 or not body:
            return

        try:
            resp_data = json.loads(body.decode('utf-8'))
            completions = resp_data['completions'] or []
            with cls._lock:
                cls._received_completions = completions
            cls._run_auto_complete(view)
        except ValueError as ex:
            logger.log('error decoding json: {}'.format(ex))

    @staticmethod
    def _run_auto_complete(view):
        view.run_command('auto_complete', {
            'api_completions_only': True,
            'disable_auto_insert': True,
            'next_completion_if_showing': False,
        })

    @staticmethod
    def _brand_completion(symbol, hint=None):
        return ('{}\t{} ⟠'.format(symbol, hint) if hint
                else '{}\t⟠'.format(symbol))

    @staticmethod
    def _event_data(view, location):
        return {
            'filename': realpath(view.file_name()),
            'editor': 'sublime3',
            'text': view.substr(sublime.Region(0, view.size())),
            'cursor_runes': location,
        }


class EditorSignaturesListener(sublime_plugin.EventListener):
    """Listener which handles signatures by sending requests to the signatures
    endpoint and rendering the returned data.
    """

    _activated = False
    _view = None
    _call = None
    _lock = Lock()

    _template_path = 'Packages/KPP/lib/assets/function-signature-panel.html'
    _template = None
    _css_path = 'Packages/KPP/lib/assets/styles.css'
    _css = ''

    @classmethod
    def queue_signatures(cls, view, location):
        deferred.defer(cls._request_signatures,
                       view, cls._event_data(view, location))

    @classmethod
    def hide_signatures(cls, view):
        reset = False
        if cls._lock.acquire(blocking=False):
            cls._activated = False
            cls._view = None
            cls._call = None
            reset = True
            cls._lock.release()

        if reset:
            view.hide_popup()

    @classmethod
    def is_activated(cls):
        return cls._activated

    @classmethod
    def _request_signatures(cls, view, data):
        resp, body = requests.kited_post('/clientapi/editor/signatures', data)

        if resp.status != 200 or not body:
            if resp.status in (400, 404):
                cls.hide_signatures(view)
            return

        try:
            resp_data = json.loads(body.decode('utf-8'))
            calls = resp_data['calls'] or []
            if len(calls):
                call = calls[0]

                if call['callee']['kind'] == 'type':
                    call['callee']['details']['function'] = \
                        call['callee']['details']['type']['language_details']\
                            ['python']['constructor']

                # Separate out the keyword-only parameters
                func = call['callee']['details']['function']
                func.update({
                    'positional_parameters': [],
                    'keyword_only_parameters': [],
                })
                for _, param in enumerate(func['parameters'] or []):
                    param_details = param['language_details']['python']
                    if not param_details['keyword_only']:
                        func['positional_parameters'].append(param)
                    else:
                        func['keyword_only_parameters'].append(param)

                in_kwargs = call['language_details']['python']['in_kwargs']
                logger.log('call: {} index = {}'
                           .format('kwarg' if in_kwargs else 'arg',
                                   call['arg_index']))

                content = None
                if cls._lock.acquire(blocking=False):
                    cls._activated = True
                    cls._view = view
                    cls._call = call
                    content = cls._render(call)
                    cls._lock.release()

                if content is not None:
                    view.show_popup(content,
                                    flags=sublime.COOPERATE_WITH_AUTO_COMPLETE,
                                    max_width=400,
                                    on_navigate=cls._handle_link_click)

        except ValueError as ex:
            logger.log('error decoding json: {}'.format(ex))

    @classmethod
    def _render(cls, call):
        if _DEBUG or cls._template is None:
            cls._template = Template(sublime.load_resource(cls._template_path))
            cls._css = sublime.load_resource(cls._css_path)

        opts = {
            'show_popular_patterns': settings.get('show_popular_patterns'),
            'show_keyword_arguments': settings.get('show_keyword_arguments'),
            'keyword_argument_highlighted': cls._kwarg_highlighted(),
        }

        return htmlmin.minify(cls._template.render(css=cls._css, call=call,
                                                   **opts),
                              remove_all_empty_space=True)

    @classmethod
    def _rerender(cls):
        content = None
        if cls._lock.acquire(blocking=True):
            content = cls._render(cls._call)
            cls._lock.release()

        if content is not None:
            cls._view.show_popup(content,
                                 flags=sublime.COOPERATE_WITH_AUTO_COMPLETE,
                                 max_width=400,
                                 on_navigate=cls._handle_link_click)

    @classmethod
    def _handle_link_click(cls, target):
        if target == 'hide_popular_patterns':
            settings.set('show_popular_patterns', False)
            cls._rerender()
        elif target == 'show_popular_patterns':
            settings.set('show_popular_patterns', True)
            cls._rerender()
        elif target == 'hide_keyword_arguments':
            settings.set('show_keyword_arguments', False)
            cls._rerender()
        elif target == 'show_keyword_arguments':
            settings.set('show_keyword_arguments', True)
            cls._rerender()
        elif (target.startswith('open_browser') or
              target.startswith('open_copilot')):
            idx = target.find(':')
            if idx == -1:
                logger.log('invalid open link format: {}'.format(target))
                return
            action = target[:idx]
            ident = target[idx+1:]
            if action == 'open_browser':
                link_opener.open_browser(ident)
            else:
                link_opener.open_copilot(ident)

    @classmethod
    def _kwarg_highlighted(cls):
        return (cls._activated and
                cls._call['language_details']['python']['in_kwargs'] and
                cls._call['arg_index'] != -1)

    @staticmethod
    def _event_data(view, location):
        return {
            'filename': realpath(view.file_name()),
            'editor': 'sublime3',
            'text': view.substr(sublime.Region(0, view.size())),
            'cursor_runes': location,
        }
