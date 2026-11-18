"""Keyboard-only Textual screens; all terminal input belongs to the framework."""
import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys
import time

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.suggester import SuggestFromList
from textual.widgets import Checkbox, Input, OptionList, Static

from .common import Failure, safe

WIDTH = 72
MAX_LINES = 12


class Back(Exception):
    def __init__(self, values=None):
        self.values = values or {}


EDIT = 'edit-settings'


@dataclass
class Field:
    key: str
    label: str
    value: str = ''
    secret: bool = False  # Visible, but cleared before leaving; never retained.
    limit: int = 256
    valid: object = None
    error: str = 'Invalid value; enter it again'
    suggestions: tuple = ()


def line(value, style=None):
    return Text(safe(value), style=style, no_wrap=True, overflow='ellipsis')


class Dialog(Screen):
    BINDINGS = [Binding('escape', 'back', show=False)]

    def __init__(self, title, *, rows=(), context=None, back=True, step=''):
        super().__init__()
        self.title_text, self.rows, self.context = title, tuple(rows), context
        self.can_back, self.step = back, step
        if context and len(context) > 80:
            raise ValueError('context exceeds text budget')

    def heading(self):
        title = line(self.title_text, '#69d8dc')
        if self.step:
            title.append('  ' + self.step, style='#8a95a6')
        yield Static(title, classes='heading')
        yield Static('-' * WIDTH, classes='rule')
        if self.context:
            yield Static(line(self.context), classes='body-line')
        for row in self.rows:
            color = '#e6c07b' if str(row).startswith('! ') else '#f08a8a' if str(row).startswith('x ') else None
            yield Static(line(row, color), classes='body-line')

    def footer(self, text='Enter continue'):
        return Static(line(text + ('   Esc back' if self.can_back else '') + '   Ctrl-C power off'), classes='footer')

    def action_back(self):
        if self.can_back:
            self.dismiss(Back())

    def on_mount(self):
        self.call_after_refresh(self.app.signal_ready)


class FormScreen(Dialog):
    BINDINGS = Dialog.BINDINGS + [Binding('f2', 'edit', show=False)]

    def __init__(self, title, fields, *, validate=None, editable=False, compact=False, **kwargs):
        super().__init__(title, **kwargs)
        self.fields, self.validate_form, self.editable = fields, validate, editable
        self.compact = compact
        if 2 + bool(self.context) + len(self.rows) + len(fields) + (1 if compact else 2) > MAX_LINES:
            raise ValueError('form exceeds twelve lines')

    def compose(self) -> ComposeResult:
        with Vertical(classes='canvas'):
            yield from self.heading()
            for field in self.fields:
                with Horizontal(classes='field-row'):
                    yield Static(line('> ' + field.label, '#69d8dc'), classes='field-label')
                    yield Input(value=field.value, id=field.key, max_length=field.limit,
                                select_on_focus=True, compact=True,
                                suggester=SuggestFromList(field.suggestions) if field.suggestions else None)
            yield Static('', id='error', classes='error')
            footer = self.footer('Enter install   F2 edit' if self.compact else
                                 'Enter continue' + ('   F2 edit' if self.editable else ''))
            footer.id = 'form-footer'
            yield footer

    def on_mount(self):
        super().on_mount()
        if self.compact:
            self.query_one('#error', Static).display = False

    def show_error(self, message):
        error = self.query_one('#error', Static)
        error.update(line('x ' + message, '#f08a8a'))
        if self.compact:
            error.display = True
            self.query_one('#form-footer', Static).display = False

    def on_input_changed(self, event: Input.Changed):
        if self.compact:
            self.query_one('#error', Static).display = False
            self.query_one('#form-footer', Static).display = True

    def values(self, *, public=False):
        return {f.key: self.query_one('#' + f.key, Input).value
                for f in self.fields if not public or not f.secret}

    def wipe(self):
        for field in self.fields:
            if field.secret:
                self.query_one('#' + field.key, Input).value = ''
                field.value = ''

    def on_input_submitted(self, event: Input.Submitted):
        event.stop()
        index = next(i for i, f in enumerate(self.fields) if f.key == event.input.id)
        field = self.fields[index]
        if field.valid and not field.valid(event.value):
            self.show_error(field.error)
            return
        if index < len(self.fields) - 1:
            self.query_one('#' + self.fields[index + 1].key, Input).focus()
            return
        values = self.values()
        for field in self.fields:
            if field.valid and not field.valid(values[field.key]):
                self.show_error(field.error)
                self.query_one('#' + field.key, Input).focus()
                return
        problem = self.validate_form(values) if self.validate_form else None
        if problem:
            self.show_error(problem)
            return
        self.wipe()
        self.dismiss(values)

    def action_back(self):
        if self.can_back:
            values = self.values(public=True)
            self.wipe()
            self.dismiss(Back(values))

    def action_edit(self):
        if self.editable:
            self.wipe()
            self.dismiss(EDIT)


class MenuScreen(Dialog):
    BINDINGS = Dialog.BINDINGS + [
        Binding('n,pagedown', 'page(1)', show=False),
        Binding('p,pageup', 'page(-1)', show=False),
        Binding('tab', 'move(1)', show=False),
        Binding('shift+tab', 'move(-1)', show=False),
        Binding('down', 'move(1)', priority=True, show=False),
        Binding('up', 'move(-1)', priority=True, show=False),
    ]
    PAGE_SIZE = 7

    def __init__(self, title, items, *, default=0, **kwargs):
        super().__init__(title, **kwargs)
        if not items:
            raise ValueError('menu requires an item')
        self.items = items
        self.selected = min(max(default, 0), len(items) - 1)
        self.page = self.selected // self.PAGE_SIZE

    def prompts(self):
        base = self.page * self.PAGE_SIZE
        return [line(f'{i + 1}  {value}') for i, value in enumerate(self.items[base:base + self.PAGE_SIZE])]

    def compose(self) -> ComposeResult:
        with Vertical(classes='canvas'):
            yield from self.heading()
            yield OptionList(*self.prompts(), id='choices', compact=True)
            if len(self.items) > self.PAGE_SIZE:
                yield Static('', id='page', classes='meta')
            yield self.footer('Up/Down select   Enter continue')

    def on_mount(self):
        super().on_mount()
        self.query_one(OptionList).highlighted = self.selected % self.PAGE_SIZE
        self.update_page()

    def update_page(self):
        if len(self.items) > self.PAGE_SIZE:
            count = (len(self.items) + self.PAGE_SIZE - 1) // self.PAGE_SIZE
            self.query_one('#page', Static).update(line(f'n/p page   {self.page + 1}/{count}', '#8a95a6'))

    def action_page(self, direction):
        page = self.page + direction
        if 0 <= page <= (len(self.items) - 1) // self.PAGE_SIZE:
            self.page = page
            choices = self.query_one(OptionList)
            choices.clear_options().add_options(self.prompts())
            choices.highlighted = 0
            self.update_page()

    def action_move(self, direction):
        choices = self.query_one(OptionList)
        index = (choices.highlighted or 0) + direction
        if 0 <= index < choices.option_count:
            choices.highlighted = index
        elif direction > 0:
            self.action_page(1)
        elif self.page:
            self.action_page(-1)
            choices.highlighted = choices.option_count - 1

    def on_key(self, event):
        if event.character and event.character in '1234567':
            index = int(event.character) - 1
            choices = self.query_one(OptionList)
            if index < choices.option_count:
                choices.highlighted = index
                event.stop()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        event.stop()
        self.dismiss(self.page * self.PAGE_SIZE + event.option_index)


class BusyScreen(Dialog):
    def __init__(self, title, message, **kwargs):
        super().__init__(title, back=False, **kwargs)
        self.message, self.started = message, time.monotonic()

    def compose(self) -> ComposeResult:
        with Vertical(classes='canvas'):
            yield from self.heading()
            yield Static('', id='progress', classes='body-line')
            yield Static(line('Ctrl-C power off', '#8a95a6'), classes='footer')

    def on_mount(self):
        self.tick()
        self.set_interval(1, self.tick)

    def tick(self):
        elapsed = int(time.monotonic() - self.started)
        self.query_one('#progress', Static).update(line(f'- {self.message}  {elapsed // 60:02}:{elapsed % 60:02}'))


class NoticeScreen(Dialog):
    BINDINGS = Dialog.BINDINGS + [Binding('enter', 'accept', show=False)]

    def __init__(self, title, *, action='continue', **kwargs):
        super().__init__(title, **kwargs)
        self.action_text = action

    def compose(self) -> ComposeResult:
        with Vertical(classes='canvas'):
            yield from self.heading()
            yield self.footer('Enter ' + self.action_text)

    def action_accept(self):
        self.dismiss('Enter')


class CheckOption(Checkbox):
    def render(self):
        return line(f"[{'X' if self.value else ' '}] {self.label.plain}")


class SSHAccessScreen(Dialog):
    BINDINGS = Dialog.BINDINGS + [Binding('enter', 'submit', priority=True, show=False)]

    def __init__(self, key_count, *, disable_password=True, **kwargs):
        super().__init__('SSH access', rows=[f'Public keys {key_count}'], **kwargs)
        self.key_count, self.disable_password = key_count, disable_password

    def compose(self) -> ComposeResult:
        with Vertical(classes='canvas'):
            yield from self.heading()
            yield CheckOption('Disable SSH password login', self.disable_password,
                              id='disable-password', compact=True)
            yield Static('', id='error', classes='error')
            yield self.footer('Space toggle   Enter continue')

    def on_checkbox_changed(self, event: Checkbox.Changed):
        self.query_one('#error', Static).update('')

    def action_submit(self):
        disabled = self.query_one(Checkbox).value
        if disabled and not self.key_count:
            self.query_one('#error', Static).update(line('x Add an SSH key or allow SSH password login', '#f08a8a'))
            return
        self.dismiss(disabled)

    def action_back(self):
        self.dismiss(Back({'disable_ssh_password': self.query_one(Checkbox).value}))


class UI:
    def __init__(self, app):
        self.app, self.step = app, ''
        self.frame = ('Installer', (), None)

    def screen(self, title, rows=(), context=None):
        self.frame = (title, tuple(rows), context)

    async def dialog(self, screen):
        result = await self.app.push_screen_wait(screen)
        if isinstance(result, Back):
            raise result
        return result

    async def form(self, title, fields, *, context=None, rows=(), back=True, validate=None, editable=False,
                   compact=False):
        return await self.dialog(FormScreen(title, fields, context=context, rows=rows, back=back,
                                            step=self.step, validate=validate, editable=editable, compact=compact))

    async def read(self, label, default=None, *, secret=False, limit=256, timeout=None, back=True,
                   valid=None, error='Invalid value; enter it again', editable=False):
        title, rows, context = self.frame
        if default == 'Enter' and not secret and valid is None:
            return await self.dialog(NoticeScreen(title, rows=rows, context=context, back=back,
                                                   step=self.step, action=label.lower()))
        dialog = self.form(title, [Field('value', label, str(default) if default is not None else '',
                                         secret, limit, valid, error)],
                           context=context, rows=rows, back=back, editable=editable)
        try:
            values = await asyncio.wait_for(dialog, timeout) if timeout else await dialog
        except TimeoutError:
            await self.app.pop_screen()
            raise Failure('Input timed out; retry') from None
        return EDIT if values == EDIT else values['value']

    async def ask(self, title, label, default=None, *, context=None, secret=False, valid=None,
                  limit=256, error='Invalid value; enter it again', back=True):
        self.screen(title, context=context)
        return await self.read(label, default, secret=secret, valid=valid, limit=limit, error=error, back=back)

    async def menu(self, title, items, *, context=None, default=0, back=True):
        return await self.dialog(MenuScreen(title, items, context=context, default=default, back=back, step=self.step))

    async def ssh_access(self, key_count, *, disable_password=True):
        return await self.dialog(SSHAccessScreen(key_count, disable_password=disable_password, step=self.step))

    def run_editor(self, command, **kwargs):
        with self.app.suspend():
            result = subprocess.run(command, stdin=sys.__stdin__, stdout=sys.__stdout__,
                                    stderr=sys.__stderr__, **kwargs)
            if result.returncode in (130, -2):
                self.app.action_abort()
            return result

    async def notice(self, title, message, warning=False, *, back=False):
        await self.dialog(NoticeScreen(title, rows=[('! ' if warning else 'x ') + message],
                                        back=back, step=self.step))

    async def busy(self, title, message, function, *args, rows=(), context=None, **kwargs):
        screen = BusyScreen(title, message, step=self.step, rows=rows, context=context)
        await self.app.push_screen(screen)
        try:
            return await asyncio.to_thread(function, *args, **kwargs)
        finally:
            if not self.app.cancelled and self.app.is_running and screen.is_mounted:
                await self.app.pop_screen()


class KioskApp(App):
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [Binding('ctrl+c', 'abort', priority=True, show=False),
                Binding('ctrl+q', 'abort', priority=True, show=False)]
    CSS = '''
    Screen { background: #171b20; color: #dce3eb; align: center middle; overflow: hidden hidden; }
    .canvas { width: 72; max-width: 100%; height: auto; max-height: 12; overflow: hidden hidden; }
    Static { height: 1; overflow: hidden hidden; }
    .heading { color: #69d8dc; }
    .rule, .meta, .footer { color: #8a95a6; }
    .error { color: #f08a8a; }
    .field-row { height: 1; }
    .field-label { width: 14; }
    Input { width: 1fr; height: 1; padding: 0; border: none; background: #171b20; color: #dce3eb; }
    Input:focus { border: none; background: #26343d; }
    Input.-invalid { border: none; }
    OptionList { height: auto; max-height: 7; padding: 0; border: none; background: #171b20; overflow: hidden hidden; }
    OptionList:focus { border: none; }
    OptionList > .option-list--option { padding: 0; }
    OptionList > .option-list--option-highlighted { background: #26343d; color: #69d8dc; text-style: bold; }
    Checkbox { height: 1; border: none; padding: 0; background: #171b20; }
    Checkbox:focus { color: #69d8dc; background: #26343d; }
    '''

    def __init__(self, workflow, *, on_abort=None):
        super().__init__()
        self.workflow, self.ui = workflow, UI(self)
        self.on_abort = on_abort
        self.failure, self.cancelled, self.ready_sent = None, False, False

    def on_mount(self):
        self.run_worker(self.run_workflow(), exit_on_error=False)

    async def run_workflow(self):
        return_code = 0
        try:
            return_code = await self.workflow(self.ui) or 0
        except asyncio.CancelledError:
            return
        except Exception as error:
            self.failure = error
            await self.ui.notice('Installer', str(error) if isinstance(error, Failure) else
                                 'Cannot continue; inspect the live logs in recovery')
        # Installation state contains credentials; never return it to Textual's
        # optional return-value/debug rendering.
        self.exit(return_code=return_code)

    def action_abort(self):
        self.cancelled = True
        for field in self.screen.query(Input):
            field.value = ''
        self.workers.cancel_all()
        # asyncio.run waits for thread-pool shutdown. A live abort must request
        # power-off before that wait, including a blocked NetworkManager call.
        if self.on_abort:
            self.on_abort()
        self.exit()

    def action_quit(self):
        self.action_abort()

    def signal_ready(self):
        if not self.ready_sent and os.environ.get('WAYLAND_DISPLAY') and Path('/dev/virtio-ports/org.kiosk.smoke').exists():
            self.ready_sent = True
            self.run_worker(self.write_ready(), exit_on_error=False)

    async def write_ready(self):
        from .common import output
        try:
            state = await asyncio.to_thread(output, ['mokutil', '--sb-state'], check=False)
            if 'SecureBoot enabled' in state:
                Path('/dev/virtio-ports/org.kiosk.smoke').write_text('KIOSK_READY SB=enabled\n')
        except (OSError, Failure):
            pass
