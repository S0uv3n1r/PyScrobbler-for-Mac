# -*- coding: utf-8 -*-
"""
Music Scrobbler（整合修补版）
------------------------------------------------------------
本版在你的原始 GUI 基础上进行了以下修补与增强：
1) 首次无 ListenBrainz Token -> 必填弹窗采集；
2) Token/会话/最近一次提交 持久化保存；
3) GUI 中提供“⚙️ 设置”入口，随时修改 LB Token；
4) 防重复提交：
   - 同一进程内：暂停后重新开始不会重复；
   - 跨重启：把“最近一次提交曲目”写入配置，启动时读取，避免再次提交；
5) macOS 打包 .app 后的“只读文件系统”问题：
   - 所有数据文件（scrobbler_config.json、lastfm_session.json）统一写入
     用户可写的 Application Support（或等价平台目录）。

依赖：customtkinter、requests
安装：pip install customtkinter requests
运行：python music_scrobbler_integrated_fixed_YYYY-MM-DD_HHMMSS.py
"""

import customtkinter as ctk
import threading
import time
import requests
import json
import subprocess
import platform
import webbrowser
import hashlib
import os
from pathlib import Path

# ----------------------------
# 平台可写目录（Application Support / AppData / XDG）
# ----------------------------
APP_NAME = "MusicScrobbler"

def get_app_support_dir(app_name: str = APP_NAME) -> Path:
    sysname = platform.system()
    if sysname == "Darwin":
        base = Path.home() / "Library" / "Application Support" / app_name
    elif sysname == "Windows":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / app_name
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / app_name
    base.mkdir(parents=True, exist_ok=True)
    return base

APP_SUPPORT_DIR = get_app_support_dir()

# 旧位置（脚本同目录）用于一次性迁移
APP_DIR = Path(__file__).resolve().parent
OLD_CONFIG_FILE = APP_DIR / 'scrobbler_config.json'

# 统一数据文件路径（可写）
CONFIG_FILE = APP_SUPPORT_DIR / 'scrobbler_config.json'
LASTFM_SESSION_FILE = APP_SUPPORT_DIR / 'lastfm_session.json'

# 如存在旧配置但新路径不存在 -> 迁移一次
if OLD_CONFIG_FILE.exists() and not CONFIG_FILE.exists():
    try:
        CONFIG_FILE.write_text(OLD_CONFIG_FILE.read_text(encoding='utf-8'), encoding='utf-8')
    except Exception as _e:
        print(f"迁移旧配置失败：{_e}")

# ----------------------------
# 配置结构
# ----------------------------
DEFAULT_CONFIG = {
    # ListenBrainz 用户令牌（Token）
    "listenbrainz_token": "",
    # 最近一次成功提交（用于跨重启防重复）
    "last_submitted_artist": "",
    "last_submitted_track": ""
}

def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            with CONFIG_FILE.open('r', encoding='utf-8') as f:
                data = json.load(f)
            merged = DEFAULT_CONFIG.copy()
            # 仅合并默认里声明的键，避免脏键
            for k in DEFAULT_CONFIG:
                if k in data:
                    merged[k] = data[k]
            return merged
        except Exception as e:
            print(f"配置读取失败：{e}，使用默认配置。")
            return DEFAULT_CONFIG.copy()
    return DEFAULT_CONFIG.copy()

def save_config(cfg: dict):
    try:
        with CONFIG_FILE.open('w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"配置保存失败：{e}")

# ----------------------------
# Last.fm API（沿用你的现有凭据/流程）
# ----------------------------
LASTFM_API_KEY = "81d57900d5fed7849e9df2538ca4efac"
LASTFM_SHARED_SECRET = "a7545c698695d5a0720c1e966060b122"

# CTk 主题
ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")


class SettingsDialog(ctk.CTkToplevel):
    """ListenBrainz Token 设置弹窗（支持显示/隐藏）。"""
    def __init__(self, master, current_token: str, on_save):
        super().__init__(master)
        self.title("⚙️ 设置：ListenBrainz Token")
        self.geometry("420x230")
        self.resizable(False, False)
        self._on_save = on_save

        frm = ctk.CTkFrame(self)
        frm.pack(fill="both", expand=True, padx=16, pady=16)

        ctk.CTkLabel(frm, text="ListenBrainz Token", anchor="w").pack(fill="x")

        self._var = ctk.StringVar(value="")
        self._entry = ctk.CTkEntry(frm, textvariable=self._var, width=360, show="•")
        self._entry.pack(pady=(6, 0))
        self._entry.focus_set()

        # 当前状态
        if (current_token or "").strip():
            ctk.CTkLabel(frm, text="当前：已设置（不显示具体值）", text_color="#198754").pack(anchor="w", pady=(6,0))
        else:
            ctk.CTkLabel(frm, text="当前：未设置", text_color="#dc3545").pack(anchor="w", pady=(6,0))

        # 显示/隐藏
        self._show_var = ctk.BooleanVar(value=False)
        def toggle_show():
            self._entry.configure(show="" if self._show_var.get() else "•")
        ctk.CTkCheckBox(frm, text="显示字符", variable=self._show_var, command=toggle_show).pack(anchor="w", pady=(8, 8))

        # 按钮区
        btn_row = ctk.CTkFrame(frm)
        btn_row.pack(fill="x", pady=(8,0))

        def on_ok():
            new_val = (self._var.get() or "").strip()
            # 允许留空 -> 保持不变，由回调处理
            self._on_save(new_val)
            self.destroy()

        def on_cancel():
            self.destroy()

        ctk.CTkButton(btn_row, text="保存", width=120, command=on_ok).pack(side="right")
        ctk.CTkButton(btn_row, text="取消", width=120, fg_color="#6c757d", hover_color="#5c636a", command=on_cancel).pack(side="right", padx=(0,8))


class ForceTokenDialog(ctk.CTkToplevel):
    """首次运行强制设置 Token 的弹窗（必须非空）。"""
    def __init__(self, master, on_save_required):
        super().__init__(master)
        self.title("设置 ListenBrainz Token")
        self.geometry("420x200")
        self.resizable(False, False)
        self._on_save_required = on_save_required

        frm = ctk.CTkFrame(self)
        frm.pack(fill="both", expand=True, padx=16, pady=16)

        ctk.CTkLabel(frm, text="首次使用需要设置 ListenBrainz Token：", anchor="w").pack(fill="x")

        self._var = ctk.StringVar(value="")
        self._entry = ctk.CTkEntry(frm, textvariable=self._var, width=360, show="•")
        self._entry.pack(pady=(6, 0))
        self._entry.focus_set()

        self._show_var = ctk.BooleanVar(value=False)
        def toggle_show():
            self._entry.configure(show="" if self._show_var.get() else "•")
        ctk.CTkCheckBox(frm, text="显示字符", variable=self._show_var, command=toggle_show).pack(anchor="w", pady=(8, 8))

        btn_row = ctk.CTkFrame(frm)
        btn_row.pack(fill="x", pady=(8,0))

        self._msg = ctk.CTkLabel(frm, text="", text_color="#dc3545")
        self._msg.pack(anchor="w", pady=(6,0))

        def on_ok():
            val = (self._var.get() or "").strip()
            if not val:
                self._msg.configure(text="Token 不能为空")
                return
            self._on_save_required(val)
            self.destroy()

        def on_cancel():
            # 首次必须设置；取消仅关闭弹窗
            self.destroy()

        ctk.CTkButton(btn_row, text="保存", width=120, command=on_ok).pack(side="right")
        ctk.CTkButton(btn_row, text="取消", width=120, fg_color="#6c757d", hover_color="#5c636a", command=on_cancel).pack(side="right", padx=(0,8))


class ScrobbleApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("🎵 Music Scrobbler")
        self.geometry("560x470")

        # 配置
        self.cfg = load_config()

        # 顶部信息
        self.song_label = ctk.CTkLabel(self, text="当前歌曲：暂无", font=("Arial", 16))
        self.song_label.pack(pady=16)

        self.lb_status = ctk.CTkLabel(self, text="ListenBrainz 状态：未提交", text_color="gray")
        self.lb_status.pack(pady=6)

        self.lfm_status = ctk.CTkLabel(self, text="Last.fm 状态：未登录", text_color="gray")
        self.lfm_status.pack(pady=6)

        # 操作按钮
        btn_row = ctk.CTkFrame(self)
        btn_row.pack(pady=10)

        self.login_button = ctk.CTkButton(btn_row, text="🔐 登录 Last.fm", command=self.login_lastfm)
        self.login_button.grid(row=0, column=0, padx=6)

        self.start_button = ctk.CTkButton(btn_row, text="▶️ 开始监听", command=self.start_listening)
        self.start_button.grid(row=0, column=1, padx=6)

        self.stop_button = ctk.CTkButton(btn_row, text="🛑 停止监听", command=self.stop_listening)
        self.stop_button.grid(row=0, column=2, padx=6)

        # 设置入口（编辑 LB Token）
        self.settings_button = ctk.CTkButton(self, text="⚙️ 设置", command=self.open_settings)
        self.settings_button.pack(pady=8)

        # 监听线程状态
        self.listening = False
        self.listener_thread = None

        # 最近一次（跨重启）防重复：从配置恢复
        self.last_artist = (self.cfg.get("last_submitted_artist") or "").strip() or None
        self.last_track  = (self.cfg.get("last_submitted_track")  or "").strip() or None

        # Last.fm session
        self.lastfm_session_key = self.load_lastfm_session()
        if self.lastfm_session_key:
            self.lfm_status.configure(text="Last.fm 状态：已登录", text_color="green")

        # 首次确保 Token
        self.after(200, self.ensure_listenbrainz_token_first_run)

    # ========== ListenBrainz Token 管理 ==========
    def ensure_listenbrainz_token_first_run(self):
        token = (self.cfg.get('listenbrainz_token') or '').strip()
        if not token:
            def on_save_required(val: str):
                self.cfg['listenbrainz_token'] = val.strip()
                save_config(self.cfg)
                self.lb_status.configure(text="ListenBrainz 状态：已设置 Token", text_color="#198754")
            ForceTokenDialog(self, on_save_required)

    def open_settings(self):
        current = (self.cfg.get('listenbrainz_token') or '').strip()
        def on_save(new_val: str):
            if new_val:  # 留空=不变
                self.cfg['listenbrainz_token'] = new_val
                save_config(self.cfg)
                self.lb_status.configure(text="ListenBrainz 状态：Token 已更新", text_color="#198754")
        SettingsDialog(self, current, on_save)

    def get_lb_token(self) -> str:
        return (self.cfg.get('listenbrainz_token') or '').strip()

    # ========== Last.fm 登录相关 ==========
    def login_lastfm(self):
        token_url = f"http://ws.audioscrobbler.com/2.0/?method=auth.getToken&api_key={LASTFM_API_KEY}&format=json"
        try:
            response = requests.get(token_url)
            token = response.json().get("token")
            if token:
                auth_url = f"http://www.last.fm/api/auth/?api_key={LASTFM_API_KEY}&token={token}"
                webbrowser.open(auth_url)
                self.after(1000, lambda: self.prompt_token(token))
            else:
                self.lfm_status.configure(text="Last.fm 状态：获取 token 失败", text_color="red")
        except Exception as e:
            self.lfm_status.configure(text=f"Last.fm 状态：错误 {e}", text_color="red")

    def prompt_token(self, token):
        def confirm():
            self.get_lastfm_session(token)
            popup.destroy()

        popup = ctk.CTkToplevel(self)
        popup.geometry("300x150")
        popup.title("授权确认")
        label = ctk.CTkLabel(popup, text="请在浏览器中授权后点击确认")
        label.pack(pady=20)
        confirm_button = ctk.CTkButton(popup, text="✅ 确认授权", command=confirm)
        confirm_button.pack(pady=10)

    def get_lastfm_session(self, token):
        params = {
            "method": "auth.getSession",
            "api_key": LASTFM_API_KEY,
            "token": token,
        }
        api_sig = self.generate_api_sig(params)
        params["api_sig"] = api_sig
        params["format"] = "json"
        try:
            response = requests.get("http://ws.audioscrobbler.com/2.0/", params=params)
            session = response.json().get("session")
            if session:
                self.lastfm_session_key = session["key"]
                self.save_lastfm_session(self.lastfm_session_key)
                self.lfm_status.configure(text="Last.fm 状态：已登录", text_color="green")
            else:
                self.lfm_status.configure(text="Last.fm 状态：获取 session 失败", text_color="red")
        except Exception as e:
            self.lfm_status.configure(text=f"Last.fm 状态：错误 {e}", text_color="red")

    def generate_api_sig(self, params):
        sorted_items = sorted(params.items())
        sig_str = "".join(f"{k}{v}" for k, v in sorted_items) + LASTFM_SHARED_SECRET
        return hashlib.md5(sig_str.encode()).hexdigest()

    def save_lastfm_session(self, key):
        try:
            with LASTFM_SESSION_FILE.open("w", encoding="utf-8") as f:
                json.dump({"session_key": key}, f, ensure_ascii=False)
        except Exception as e:
            self.lfm_status.configure(text=f"Last.fm 状态：保存会话失败 {e}", text_color="red")

    def load_lastfm_session(self):
        try:
            with LASTFM_SESSION_FILE.open("r", encoding="utf-8") as f:
                return json.load(f).get("session_key")
        except Exception:
            return None

    # ========== 监听 & 提交 ==========
    def start_listening(self):
        if not self.listening:
            self.listening = True
            self.listener_thread = threading.Thread(target=self.listen_loop, daemon=True)
            self.listener_thread.start()
            self.lb_status.configure(text="ListenBrainz 状态：监听中", text_color="blue")

    def stop_listening(self):
        self.listening = False
        self.lb_status.configure(text="ListenBrainz 状态：已停止", text_color="gray")

    def get_apple_music_info(self):
        if platform.system() != "Darwin":
            return None, None
        try:
            # 使用不易冲突的分隔符，避免转义/换行问题
            sentinel = "|||COPILOT|||"
            script = f"""
            tell application "Music"
                if player state is playing then
                    return artist of current track & "{sentinel}" & name of current track
                else
                    return ""
                end if
            end tell
            """
            process = subprocess.Popen(['osascript', '-e', script], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdout, stderr = process.communicate()
            if stderr and stderr.decode().strip():
                print(f"AppleScript error: {stderr.decode().strip()}")
                return None, None
            stdout_decoded = stdout.decode().strip()
            if sentinel not in stdout_decoded or stdout_decoded == "":
                return None, None
            artist_name, track_name = stdout_decoded.split(sentinel, 1)
            return artist_name.strip(), track_name.strip()
        except Exception as e:
            print(f"❌ Exception occurred while getting track info: {e}")
            return None, None

    def listen_loop(self):
        last_submitted_artist = None
        last_submitted_track = None
        while self.listening:
            artist, track = self.get_apple_music_info()
            if artist and track and (artist != self.last_artist or track != self.last_track):
                if artist == last_submitted_artist and track == last_submitted_track:
                    time.sleep(15)
                    continue
                timestamp = int(time.time())
                self.song_label.configure(text=f"当前歌曲：{artist} - {track}")
                self.submit_listenbrainz(artist, track, timestamp)
                self.submit_lastfm(artist, track, timestamp)
                # 本次进程内防重
                self.last_artist = artist
                self.last_track = track
                # 本轮防重（避免短期重复）
                last_submitted_artist = artist
                last_submitted_track = track
            time.sleep(15)

    def submit_listenbrainz(self, artist, track, timestamp):
        token = self.get_lb_token()
        if not token:
            self.lb_status.configure(text="ListenBrainz 状态：缺少 Token，未提交", text_color="red")
            return
        url = "https://api.listenbrainz.org/1/submit-listens"
        headers = {
            "Authorization": f"Token {token}",
            "Content-Type": "application/json",
        }
        payload = {
            "listen_type": "single",
            "payload": [
                {
                    "listened_at": timestamp,
                    "track_metadata": {
                        "artist_name": artist,
                        "track_name": track
                    }
                }
            ]
        }
        try:
            response = requests.post(url, headers=headers, data=json.dumps(payload))
            response.raise_for_status()
            self.lb_status.configure(text="ListenBrainz 状态：已提交", text_color="green")
            # 跨重启防重：写入配置
            self.cfg["last_submitted_artist"] = artist
            self.cfg["last_submitted_track"] = track
            save_config(self.cfg)
        except Exception as e:
            self.lb_status.configure(text=f"ListenBrainz 错误：{e}", text_color="red")

    def submit_lastfm(self, artist, track, timestamp):
        if not self.lastfm_session_key:
            return
        params = {
            "method": "track.scrobble",
            "artist": artist,
            "track": track,
            "timestamp": str(timestamp),
            "api_key": LASTFM_API_KEY,
            "sk": self.lastfm_session_key,
        }
        api_sig = self.generate_api_sig(params)
        params["api_sig"] = api_sig
        params["format"] = "json"
        try:
            response = requests.post("http://ws.audioscrobbler.com/2.0/", data=params)
            response.raise_for_status()
            self.lfm_status.configure(text="Last.fm 状态：已提交", text_color="green")
        except Exception as e:
            self.lfm_status.configure(text=f"Last.fm 错误：{e}", text_color="red")


if __name__ == '__main__':
    try:
        app = ScrobbleApp()
        app.mainloop()
    except Exception as ex:
        import sys
        sys.stderr.write("\n程序异常：" + str(ex) + "\n")
        raise
