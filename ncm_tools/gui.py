from __future__ import annotations

import contextlib
import json
import os
import queue
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from .api import ApiClient, ApiError
from .auth import find_edge, load_cookies, login_qr, parse_cookies, save_cookies
from .runner import CHINA, atomic_json, run_batch, run_repeat
from .repeat import parse_song_ids, run_multi_repeat, run_playlist_rounds
from .__main__ import USER_ROOT, local_api

BG = '#f3f5fa'
INK = '#17243a'
MUTED = '#64748b'
ACCENT = '#d72d4b'


class LogWriter:
    def __init__(self, events):
        self.events = events

    def write(self, text):
        if text:
            self.events.put(('log', text))

    def flush(self):
        pass


class Application:
    def __init__(self, root, state_dir=None):
        self.root = root
        self.state_dir = Path(state_dir or USER_ROOT / '.state')
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.closing = False
        self.api_context = None
        self.api_url = None
        self.client = None
        self.worker = None
        self.buttons = []
        self.inputs = []
        try:
            settings = json.loads((self.state_dir / 'settings.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            settings = {}
        self.settings = settings
        self.api_var = tk.StringVar(value=settings.get('api', 'http://127.0.0.1:3000'))
        self.playlist_var = tk.StringVar(value=str(settings.get('playlist', '')))
        self.count_var = tk.StringVar(value=str(settings.get('count', 20)))
        self.song_var = tk.StringVar(value=str(settings.get('song_id', '')))
        self.repeat_var = tk.StringVar(value=str(settings.get('repetitions', 1)))
        self.mode_var = tk.StringVar(value=settings.get('mode', '歌单听歌量'))
        if self.mode_var.get() == '单曲播放次数':
            self.mode_var.set('单曲/多曲次数')
        self.rounds_var = tk.StringVar(value=str(settings.get('rounds', 1)))
        self.parallel_var = tk.StringVar(value=str(settings.get('concurrency', 3)))
        self.plan_var = tk.StringVar(value='')
        self.delta_label_var = tk.StringVar(value='本轮实际增量')
        self.interval_var = tk.StringVar(value=str(settings.get('interval', 2)))
        self.verify_var = tk.StringVar(value=str(settings.get('verify', 15)))
        self.endpoint_var = tk.StringVar(value=settings.get('endpoint', '自动选择'))
        self.hour_var = tk.StringVar(value=str(settings.get('hour', 9)))
        # A recurring task starts only when explicitly selected in this app session.
        self.schedule_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value='准备就绪')
        self.account_var = tk.StringVar(value='尚未登录 · 请先扫码登录')
        self.total_var = tk.StringVar(value='—')
        self.level_var = tk.StringVar(value='—')
        self.accepted_var = tk.StringVar(value='—')
        self.delta_var = tk.StringVar(value='—')
        self._build()
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(100, self.pump)
        root.after(1000, self.schedule_tick)
        self.log('就绪。点击「扫码登录」，在 Edge 中用网易云音乐 App 扫码确认。\n')
        self.log('每轮数量可自定义，最多 250 首；上报受理与实际听歌量增长分别统计。\n')
        self.log('单曲/多曲模式支持多个歌曲 ID；每首每轮 1–100 次，循环轮数可自定义。\n')
        for variable in (self.song_var, self.count_var, self.repeat_var, self.rounds_var, self.parallel_var):
            variable.trace_add('write', lambda *_: self.update_plan())
        self.update_plan()

    def _build(self):
        root = self.root
        root.title('网易云音乐助手 · Edge 兼容版')
        root.geometry('1040x860')
        root.minsize(980, 800)
        root.configure(bg=BG)
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('.', font=('Microsoft YaHei UI', 10), foreground=INK)
        style.configure('TFrame', background=BG)
        style.configure('Card.TFrame', background='white')
        style.configure('TLabel', background=BG)
        style.configure('Card.TLabel', background='white')
        style.configure('Muted.TLabel', foreground=MUTED)
        style.configure('CardMuted.TLabel', background='white', foreground=MUTED)
        style.configure('TButton', padding=(14, 9), background='#e8edf5', borderwidth=0)
        style.map('TButton', background=[('active', '#dbe3ef'), ('disabled', '#eef1f5')])
        style.configure('Accent.TButton', background=ACCENT, foreground='white')
        style.map('Accent.TButton', background=[('active', '#b9233e'), ('disabled', '#e3a2ad')])
        style.configure('TEntry', padding=7)
        style.configure('TSpinbox', padding=7)
        style.configure('TCheckbutton', background=BG)
        style.configure('TCombobox', padding=6)

        outer = ttk.Frame(root, padding=(28, 22))
        outer.pack(fill='both', expand=True)
        header = ttk.Frame(outer)
        header.pack(fill='x')
        ttk.Label(header, text='网易云音乐助手', font=('Microsoft YaHei UI', 24, 'bold')).pack(side='left')
        ttk.Label(header, text='EDGE 适配版  /  3.2', foreground=ACCENT,
                  font=('Microsoft YaHei UI', 10, 'bold')).pack(side='right', pady=(12, 0))
        ttk.Label(outer, text='登录、签到与听歌统计，在一个窗口完成。', style='Muted.TLabel').pack(anchor='w', pady=(5, 18))

        account = ttk.Frame(outer, style='Card.TFrame', padding=(18, 13))
        account.pack(fill='x')
        ttk.Label(account, textvariable=self.account_var, style='Card.TLabel', width=34).pack(side='left')
        self.button(account, '扫码登录', self.login).pack(side='right', padx=(8, 0))
        self.button(account, '导入登录信息', self.import_cookie).pack(side='right')

        metrics = ttk.Frame(outer)
        metrics.pack(fill='x', pady=14)
        for index, (label, variable) in enumerate([
            ('累计听歌量', self.total_var), ('当前等级', self.level_var),
            ('本轮上报受理', self.accepted_var), ('本轮实际增量', self.delta_var)]):
            metrics.columnconfigure(index, weight=1, uniform='metric')
            card = ttk.Frame(metrics, style='Card.TFrame', padding=(18, 13))
            card.grid(row=0, column=index, sticky='nsew', padx=(0 if index == 0 else 6, 0 if index == 3 else 6))
            (ttk.Label(card, textvariable=self.delta_label_var, style='CardMuted.TLabel') if index == 3
             else ttk.Label(card, text=label, style='CardMuted.TLabel')).pack(anchor='w')
            ttk.Label(card, textvariable=variable, style='Card.TLabel',
                      font=('Microsoft YaHei UI', 25, 'bold')).pack(anchor='w', pady=(4, 0))

        form = ttk.Frame(outer, style='Card.TFrame', padding=18)
        form.pack(fill='x')
        form.columnconfigure(1, weight=1)
        form.columnconfigure(3, weight=1)
        ttk.Label(form, text='运行模式', style='Card.TLabel').grid(row=0, column=0, sticky='w', padx=(0, 12))
        self.mode_combo = ttk.Combobox(form, textvariable=self.mode_var, state='readonly',
            values=['歌单听歌量', '单曲/多曲次数'], width=24)
        self.mode_combo.grid(row=0, column=1, sticky='ew', padx=(0, 25), pady=(0, 12))
        self.mode_combo.bind('<<ComboboxSelected>>', lambda _: self.refresh_mode())
        self.inputs.append(self.mode_combo)
        ttk.Label(form, text='支持多首歌曲同时处理与循环', style='CardMuted.TLabel',
                  font=('Microsoft YaHei UI', 9)).grid(row=0, column=2, columnspan=2, sticky='w', pady=(0, 12))
        self.source_label = ttk.Label(form, text='歌单 ID', style='Card.TLabel')
        self.source_label.grid(row=1, column=0, sticky='w', padx=(0, 12))
        self.source_entry = ttk.Entry(form, textvariable=self.playlist_var, width=26)
        self.source_entry.grid(row=1, column=1, sticky='ew', padx=(0, 25))
        self.inputs.append(self.source_entry)
        self.number_label = ttk.Label(form, text='每轮歌曲数', style='Card.TLabel')
        self.number_label.grid(row=1, column=2, sticky='w', padx=(0, 12))
        self.number_input = ttk.Spinbox(form, from_=1, to=250, textvariable=self.count_var, width=13)
        self.number_input.grid(row=1, column=3, sticky='ew')
        self.inputs.append(self.number_input)
        self.source_hint = ttk.Label(form, style='CardMuted.TLabel', font=('Microsoft YaHei UI', 9))
        self.source_hint.grid(row=2, column=1, sticky='w', pady=(5, 0))
        self.number_hint = ttk.Label(form, style='CardMuted.TLabel', font=('Microsoft YaHei UI', 9))
        self.number_hint.grid(row=2, column=3, sticky='w', pady=(5, 0))
        self.song_list_button = self.button(form, '编辑歌曲列表', self.edit_songs)
        self.song_list_button.grid(row=2, column=0, sticky='w', pady=(6, 0))
        ttk.Label(form, text='循环轮数', style='Card.TLabel').grid(row=3, column=0, sticky='w', pady=(12, 0))
        round_input = ttk.Spinbox(form, from_=1, to=1000, textvariable=self.rounds_var, width=13)
        round_input.grid(row=3, column=1, sticky='ew', padx=(0, 25), pady=(12, 0))
        self.inputs.append(round_input)
        ttk.Label(form, text='同时处理首数', style='Card.TLabel').grid(row=3, column=2, sticky='w', pady=(12, 0))
        self.parallel_input = ttk.Spinbox(form, from_=1, to=8, textvariable=self.parallel_var, width=13)
        self.parallel_input.grid(row=3, column=3, sticky='ew', pady=(12, 0))
        self.inputs.append(self.parallel_input)
        ttk.Label(form, textvariable=self.plan_var, style='CardMuted.TLabel',
                  font=('Microsoft YaHei UI', 9)).grid(row=4, column=0, columnspan=4, sticky='w', pady=(10, 0))
        self.refresh_mode()

        schedule = ttk.Frame(outer)
        schedule.pack(fill='x', pady=(14, 12))
        check = ttk.Checkbutton(schedule, text='每天定时运行', variable=self.schedule_var)
        check.pack(side='left')
        self.inputs.append(check)
        hour = ttk.Spinbox(schedule, from_=0, to=23, textvariable=self.hour_var, width=4)
        hour.pack(side='left', padx=(8, 4))
        self.inputs.append(hour)
        ttk.Label(schedule, text='点（UTC+8，需保持程序开启）', style='Muted.TLabel').pack(side='left')
        self.button(schedule, '设置', self.show_settings).pack(side='right')

        actions = ttk.Frame(outer)
        actions.pack(fill='x', pady=(0, 12))
        self.button(actions, '开始上报', self.run, accent=True).pack(side='left', padx=(0, 10))
        self.button(actions, '查看统计', self.status).pack(side='left', padx=(0, 10))
        self.button(actions, '立即签到', self.signin).pack(side='left', padx=(0, 10))
        self.stop_button = ttk.Button(actions, text='停止', command=self.stop, state='disabled')
        self.stop_button.pack(side='left')
        self.button(actions, '检查连接', self.doctor).pack(side='right')

        self.progress = ttk.Progressbar(outer, mode='indeterminate')
        self.progress.pack(fill='x', pady=(0, 10))
        ttk.Label(outer, textvariable=self.status_var, style='Muted.TLabel').pack(anchor='w', pady=(0, 8))
        footer = ttk.Label(outer, text='接口受理不代表等级已增长；最终以网易云服务端统计为准。',
                  style='Muted.TLabel', font=('Microsoft YaHei UI', 9))
        footer.pack(side='bottom', anchor='w', pady=(10, 0))
        self.tabs = ttk.Notebook(outer)
        self.tabs.pack(fill='both', expand=True)
        log_frame, self.result_frame = ttk.Frame(self.tabs), ttk.Frame(self.tabs)
        self.tabs.add(log_frame, text='运行日志')
        self.tabs.add(self.result_frame, text='歌曲结果')
        self.logs = scrolledtext.ScrolledText(log_frame, height=6, wrap='word', font=('Microsoft YaHei UI', 10),
            background='white', foreground=INK, relief='flat', padx=14, pady=12, state='disabled')
        self.logs.pack(fill='both', expand=True)
        columns = ('id', 'name', 'accepted', 'failed', 'before', 'after', 'delta')
        self.results = ttk.Treeview(self.result_frame, columns=columns, show='headings', height=6)
        for name, title, width in [('id', '歌曲 ID', 110), ('name', '歌曲名称', 200),
                                   ('accepted', '上报受理', 85), ('failed', '未确认', 75),
                                   ('before', '原周次数', 85), ('after', '现周次数', 85), ('delta', '实际增量', 85)]:
            self.results.heading(name, text=title)
            self.results.column(name, width=width, minwidth=60, anchor='w' if name == 'name' else 'center')
        result_scroll = ttk.Scrollbar(self.result_frame, orient='vertical', command=self.results.yview)
        self.results.configure(yscrollcommand=result_scroll.set)
        result_scroll.pack(side='right', fill='y')
        self.results.pack(side='left', fill='both', expand=True)

    def button(self, parent, text, command, accent=False):
        button = ttk.Button(parent, text=text, command=command, style='Accent.TButton' if accent else 'TButton')
        self.buttons.append(button)
        return button

    def log(self, text):
        self.logs.configure(state='normal')
        self.logs.insert('end', text)
        self.logs.see('end')
        self.logs.configure(state='disabled')

    def refresh_mode(self):
        single = self.mode_var.get() != '歌单听歌量'
        self.source_label.configure(text='歌曲 ID 列表' if single else '歌单 ID')
        self.source_entry.configure(textvariable=self.song_var if single else self.playlist_var)
        self.number_label.configure(text='每首每轮次数' if single else '每轮歌曲数')
        self.number_input.configure(to=100 if single else 250,
                                    textvariable=self.repeat_var if single else self.count_var)
        self.source_hint.configure(text='多个 ID 用逗号、空格或换行分隔' if single else '留空使用自己的第一个歌单')
        self.number_hint.configure(text='可自定义 1–100 次' if single else '可自定义 1–250 首')
        try:
            multiple = single and len(parse_song_ids(self.song_var.get())) > 1
        except ValueError:
            multiple = False
        self.delta_label_var.set('歌曲次数增量' if multiple else '单曲次数增量' if single else '本轮实际增量')
        self.song_list_button.configure(state='normal' if single and not self.busy else 'disabled')
        self.parallel_input.configure(state='normal' if single and not self.busy else 'disabled')
        self.update_plan()

    def update_plan(self):
        try:
            rounds = int(self.rounds_var.get())
            if not 1 <= rounds <= 1000:
                raise ValueError
            if self.mode_var.get() != '歌单听歌量':
                ids = parse_song_ids(self.song_var.get())
                count = int(self.repeat_var.get())
                workers = int(self.parallel_var.get())
                if not 1 <= count <= 100 or not 1 <= workers <= 8:
                    raise ValueError
                self.plan_var.set(f'已选 {len(ids)} 首 × 每首每轮 {count} 次 × {rounds} 轮 = 计划 {len(ids) * count * rounds} 次；同时处理 {min(workers, len(ids))} 首')
            else:
                self.plan_var.set(f'计划循环 {rounds} 轮；每轮最多 {int(self.count_var.get())} 首，保留当天去重与上限')
        except (ValueError, TypeError):
            self.plan_var.set('填写歌曲列表、次数和循环轮数后，显示本次任务计划')

    def edit_songs(self):
        window = tk.Toplevel(self.root)
        window.title('编辑歌曲列表')
        window.transient(self.root)
        window.grab_set()
        frame = ttk.Frame(window, padding=18)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='每行一个歌曲 ID，也可粘贴逗号分隔的列表；重复 ID 会自动合并。').pack(anchor='w', pady=(0, 10))
        editor = scrolledtext.ScrolledText(frame, width=62, height=10, font=('Microsoft YaHei UI', 11))
        editor.pack(fill='both', expand=True)
        editor.insert('1.0', self.song_var.get().replace(',', '\n'))
        def save():
            try:
                ids = parse_song_ids(editor.get('1.0', 'end'))
            except ValueError as error:
                messagebox.showerror('歌曲列表有误', str(error), parent=window)
                return
            self.song_var.set(', '.join(map(str, ids)))
            window.destroy()
        ttk.Button(frame, text='使用这些歌曲', command=save).pack(anchor='e', pady=(12, 0))

    def configuration(self):
        try:
            single = self.mode_var.get() != '歌单听歌量'
            count = int(self.repeat_var.get() if single else self.count_var.get())
            source = (self.song_var.get() if single else self.playlist_var.get()).strip()
            playlist = None if single else int(source) if source else None
            songs = parse_song_ids(source) if single else None
            rounds = int(self.rounds_var.get())
            concurrency = int(self.parallel_var.get()) if single else 1
            interval, verify = float(self.interval_var.get()), float(self.verify_var.get())
            hour = int(self.hour_var.get())
            import math
            if (not 1 <= count <= (100 if single else 250) or (playlist is not None and playlist <= 0)
                    or not 1 <= rounds <= 1000 or not 1 <= concurrency <= 8):
                raise ValueError
            if not math.isfinite(interval) or interval < 1 or not math.isfinite(verify) or not 0 <= verify <= 600:
                raise ValueError
            if not 0 <= hour <= 23:
                raise ValueError
            ApiClient(self.api_var.get().strip())
        except (ValueError, TypeError):
            messagebox.showerror('设置有误', '歌单每轮 1–250 首；歌曲次数模式每首每轮 1–100 次。\n循环 1–1000 轮，同时处理 1–8 首；歌曲 ID 须为正整数。\n间隔至少 1 秒，核验等待 0–600 秒。', parent=self.root)
            return None
        self.settings.update(api=self.api_var.get().strip(), mode=self.mode_var.get(),
            interval=interval, verify=verify, hour=hour, endpoint=self.endpoint_var.get(), rounds=rounds)
        if single:
            self.settings.update(song_id=', '.join(map(str, songs)), repetitions=count, concurrency=concurrency)
        else:
            self.settings.update(count=count, playlist=playlist or '')
        atomic_json(self.state_dir / 'settings.json', self.settings)
        options = dict(interval=interval, verify_wait=verify, rounds=rounds,
            endpoint={'自动选择': 'auto', '新版上报': 'v1', '兼容旧版': 'legacy'}.get(self.endpoint_var.get(), 'auto'))
        return dict(options, song_ids=songs, repetitions=count, concurrency=concurrency) if single else dict(options, playlist_id=playlist, max_songs=count)

    def submit(self, title, operation, *, run_options=False):
        if self.busy:
            return
        if run_options:
            config = self.configuration()
            if config is None:
                return
        else:
            try:
                ApiClient(self.api_var.get().strip())
            except ValueError as error:
                messagebox.showerror('接口地址有误', str(error), parent=self.root)
                return
            config = {}
        url = self.api_var.get().strip()
        self.busy = True
        self.cancel.clear()
        self.status_var.set(title)
        self.progress.start(12)
        self.stop_button.configure(state='normal')
        for widget in self.buttons + self.inputs:
            widget.configure(state='disabled')

        def worker():
            try:
                with contextlib.redirect_stdout(LogWriter(self.events)), contextlib.redirect_stderr(LogWriter(self.events)):
                    if self.api_context is None or self.api_url != url:
                        old_cookies = self.client.cookies if self.client and self.api_url == url else {}
                        if self.api_context is not None:
                            self.api_context.__exit__(None, None, None)
                        self.api_context = None
                        context = local_api(url, self.state_dir)
                        context.__enter__()
                        self.api_context = context
                        self.api_url = url
                        self.client = ApiClient(url)
                        if old_cookies:
                            self.client.cookies = old_cookies
                        else:
                            try:
                                self.client.cookies = load_cookies(self.state_dir / 'auth.dat')
                            except (ValueError, RuntimeError):
                                print('已保存的登录信息不可用，请重新扫码登录。')
                    operation(config)
                self.events.put(('done', '任务已完成'))
            except KeyboardInterrupt:
                self.events.put(('done', '已停止，进度已保留'))
            except BaseException as error:
                if isinstance(error, ApiError):
                    detail = str(error)
                elif isinstance(error, (KeyError, TypeError)):
                    detail = '接口返回格式不兼容，请检查 API 版本。'
                elif isinstance(error, TimeoutError):
                    detail = str(error)
                elif isinstance(error, OSError):
                    detail = '文件或网络操作失败，请检查路径和网络连接。'
                else:
                    detail = str(error)
                self.events.put(('error', detail))
        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    def identity_event(self):
        uid, nick = self.client.identity()
        self.events.put(('account', f'{nick or "网易云用户"}  ·  UID {uid}'))
        return uid

    def login(self):
        def operation(_):
            uid, nick = login_qr(self.client, self.state_dir / 'auth.dat', timeout=180, cancel=self.cancel)
            self.events.put(('account', f'{nick or "网易云用户"}  ·  UID {uid}'))
            print('扫码登录成功，账号已通过服务端验证。')
            self.events.put(('statistics', self.client.statistics(uid)))
        self.submit('正在等待扫码登录，请查看 Edge 窗口…', operation)

    def import_cookie(self):
        file = filedialog.askopenfilename(parent=self.root, title='选择本地 Cookie 文件',
            filetypes=[('登录信息', '*.txt *.json'), ('所有文件', '*.*')])
        if not file:
            return
        def operation(_):
            cookies = parse_cookies(Path(file).read_text(encoding='utf-8-sig'))
            previous = self.client.cookies
            self.client.cookies = cookies
            try:
                uid = self.identity_event()
            except BaseException:
                self.client.cookies = previous
                raise
            save_cookies(self.state_dir / 'auth.dat', cookies)
            self.events.put(('statistics', self.client.statistics(uid)))
            print('登录信息导入并验证成功。')
        self.submit('正在验证登录信息…', operation)

    def status(self):
        def operation(_):
            uid = self.identity_event()
            statistics = self.client.statistics(uid)
            self.events.put(('statistics', statistics))
            print('服务端统计：' + json.dumps(statistics, ensure_ascii=False))
        self.submit('正在读取服务端听歌量和等级…', operation)

    def signin(self):
        def operation(_):
            self.identity_event()
            for label, result in self.client.signin().items():
                print(f'{label}：{result}')
        self.submit('正在签到…', operation)

    def run(self):
        def operation(config):
            self.identity_event()
            runner = run_multi_repeat if 'repetitions' in config else run_playlist_rounds
            report = runner(self.client, self.state_dir, cancel=self.cancel, **config)
            self.events.put(('report', report))
        self.submit('正在运行；可点击停止，等待当前请求结束…', operation, run_options=True)

    def doctor(self):
        def operation(_):
            print('Edge：' + ('已找到' if find_edge() else '未找到'))
            self.client.request('/login/status', read_only=True)
            print('本地 API 连接正常。点击扫码登录后可继续验收签到和听歌统计。')
        self.submit('正在检查 Edge 和接口连接…', operation)

    def show_settings(self):
        window = tk.Toplevel(self.root)
        window.title('连接与运行设置')
        window.resizable(False, False)
        window.transient(self.root)
        window.grab_set()
        frame = ttk.Frame(window, padding=24)
        frame.pack(fill='both', expand=True)
        for row, (label, variable) in enumerate([('API 地址', self.api_var), ('请求间隔（秒）', self.interval_var),
                                                ('核验等待（秒）', self.verify_var)]):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky='w', pady=8, padx=(0, 20))
            ttk.Entry(frame, textvariable=variable, width=35).grid(row=row, column=1, pady=8)
        ttk.Label(frame, text='上报方式').grid(row=3, column=0, sticky='w', pady=8)
        ttk.Combobox(frame, textvariable=self.endpoint_var, state='readonly',
                     values=['自动选择', '新版上报', '兼容旧版'], width=33).grid(row=3, column=1, pady=8)
        def save():
            if self.configuration() is not None:
                window.destroy()
        ttk.Button(frame, text='保存设置', command=save).grid(row=4, column=1, sticky='e', pady=(16, 0))

    def stop(self):
        self.cancel.set()
        self.status_var.set('正在停止，等待当前请求结束…')
        self.stop_button.configure(state='disabled')

    def pump(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'log':
                    self.log(value)
                elif kind == 'account':
                    self.account_var.set(value)
                elif kind == 'statistics':
                    self.total_var.set(str(value.get('listenSongs', '—')))
                    self.level_var.set('Lv.' + str(value.get('level', '—')))
                elif kind == 'report':
                    self.accepted_var.set(str(value['accepted']))
                    is_songs = value.get('mode') in {'single', 'multi'}
                    delta = value.get('playback_observed_delta') if is_songs else value['observed_delta']
                    self.delta_label_var.set('歌曲次数增量' if value.get('mode') == 'multi' else '单曲次数增量' if is_songs else '本轮实际增量')
                    self.delta_var.set(f'{delta:+d}' if delta is not None else '未核验')
                    self.total_var.set(str(value['after']['listenSongs']))
                    self.level_var.set('Lv.' + str(value['after'].get('level', '—')))
                    self.results.delete(*self.results.get_children())
                    for id_, item in value.get('songs', {}).items():
                        def shown(key):
                            number = item.get(key)
                            return number if number is not None else '未核验'
                        self.results.insert('', 'end', values=(id_, item.get('name', id_), item['accepted'],
                            item['failed'], shown('before'), shown('after'), shown('observed_delta')))
                    if value.get('mode') == 'multi':
                        self.tabs.select(self.result_frame)
                elif kind in {'done', 'error'}:
                    self.busy = False
                    self.progress.stop()
                    self.stop_button.configure(state='disabled')
                    for widget in self.buttons + self.inputs:
                        widget.configure(state='normal')
                    self.mode_combo.configure(state='readonly')
                    self.refresh_mode()
                    self.status_var.set(value if kind == 'done' else '任务未完成，请查看下方说明')
                    if kind == 'error':
                        self.log('提示：' + value + '\n')
        except queue.Empty:
            pass
        if self.closing and not self.busy:
            if self.api_context is not None:
                try:
                    self.api_context.__exit__(None, None, None)
                except Exception:
                    pass
            self.root.destroy()
            return
        self.root.after(100, self.pump)

    def schedule_tick(self):
        if not self.closing and self.schedule_var.get() and not self.busy:
            now = datetime.now(CHINA)
            try:
                hour = int(self.hour_var.get())
                day = now.date().isoformat()
                if now.hour == hour and self.settings.get('last_scheduled_day') != day:
                    self.settings['last_scheduled_day'] = day
                    atomic_json(self.state_dir / 'settings.json', self.settings)
                    self.run()
            except ValueError:
                self.schedule_var.set(False)
                self.log('定时时间无效，已关闭定时运行。\n')
        if not self.closing:
            self.root.after(1000, self.schedule_tick)

    def close(self):
        self.schedule_var.set(False)
        self.closing = True
        self.cancel.set()
        self.status_var.set('正在关闭，请等待当前请求结束…')


def main():
    if os.name == 'nt':
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (OSError, AttributeError):
            pass
    root = tk.Tk()
    Application(root)
    root.mainloop()


if __name__ == '__main__':
    main()
