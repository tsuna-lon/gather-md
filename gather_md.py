"""
gather_md.py
レポート作成支援スクリプト:
Markdownファイル内のコードブロックを抽出し、テンプレートに流し込んで完成レポートを自動生成・更新する。
"""

import base64
import csv
from dataclasses import dataclass
import hashlib
import io
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import threading
import time
import yaml
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# Windows環境での日本語コンソール文字化け防止
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 監視ルートディレクトリ（このスクリプトが存在するディレクトリ）
WATCH_ROOT = Path(__file__).resolve().parent

# 定義ファイル・テンプレートファイル候補名（優先順位順）
CONFIG_FILENAMES = ["report_config.yaml", "report_config.yml", "_report.yaml", "_report.yml"]
TEMPLATE_FILENAMES = ["_template.md", "report_template.md"]

# コードブロック抽出正規表現: ```report:<tag>\n...\n``` または ```report: <tag>\n...\n```（中身が空のブロックも許容）
# 日本語（全角数字・記号含む）や英数字など柔軟に対応
BLOCK_PATTERN = re.compile(
    r"```report:\s*([^\r\n]+?)\s*(?:\r?\n```|\r?\n(.*?)\r?\n```)",
    re.DOTALL
)

# コメント置換パターン: <!-- INSERT:tag --> ... <!-- END:tag --> または <!-- INSERT:tag -->
COMMENT_PLACEHOLDER_PATTERN = re.compile(
    r"<!--\s*INSERT:\s*([^\s>]+)\s*-->(?:.*?<!--\s*END:\s*\1\s*-->)?",
    re.DOTALL
)

# Mustache置換パターン: {{tag}} (改行や波括弧を含まない任意の文字列)
MUSTACHE_PLACEHOLDER_PATTERN = re.compile(r"\{\{([^\{\}\r\n]+?)\}\}")

# 誤編集防止用警告バナー（Obsidian Callout記法対応）
WARNING_BANNER = (
    "> [!CAUTION]\n"
    "> **【自動生成ファイル】直接編集しないでください**\n"
    "> このファイルは下書きから自動集約されています。直接編集した内容は自動復元（ロールバック）または次回更新時に上書きされます。\n\n"
)

# デフォルトテンプレート内容（新規フォルダ作成時の自動生成用）
DEFAULT_TEMPLATE_CONTENT = """---
output: "_完成レポート.md"
readonly: false
warning_banner: true
auto_rollback: true
---
# {title} レポート

{{１．はじめに}}

{{２．背景と課題}}

{{３．今後の展望}}

{{参考文献}}

---
文字数: {{char_count}} 字（空白含: {{char_count_with_spaces}} 字）
"""

# Windowsの一時フォルダ名パターン（エクスプローラー新規作成時の「新しいフォルダー」等）
TEMP_DIR_PATTERN = re.compile(r"^(新しいフォルダー|新規フォルダー|New folder)( \(\d+\))?$", re.IGNORECASE)


class SingleInstance:
    """Windows Named Mutex を用いた二重起動防止クラス。"""

    ERROR_ALREADY_EXISTS = 183

    def __init__(self, root_path: Path):
        self.root_path = root_path
        self.mutex = None
        self.already_running = False

    def acquire(self) -> bool:
        """ミューテックスの取得を試みる。既に起動している場合は False を返す。"""
        if sys.platform != "win32":
            return True
        try:
            import ctypes
            path_hash = hashlib.sha256(str(self.root_path.resolve()).lower().encode("utf-8")).hexdigest()[:16]
            mutex_name = f"Local\\gather_md_{path_hash}"
            handle = ctypes.windll.kernel32.CreateMutexW(None, False, mutex_name)
            last_error = ctypes.windll.kernel32.GetLastError()
            if last_error == self.ERROR_ALREADY_EXISTS:
                self.already_running = True
                if handle:
                    ctypes.windll.kernel32.CloseHandle(handle)
                return False
            self.mutex = handle
            return True
        except Exception as e:
            print(f"[警告] 二重起動防止ミューテックスの初期化に失敗しました: {e}", flush=True)
            return True

    def release(self) -> None:
        """ミューテックスを解放する。"""
        if sys.platform == "win32" and self.mutex:
            try:
                import ctypes
                ctypes.windll.kernel32.CloseHandle(self.mutex)
            except Exception:
                pass
            self.mutex = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


def send_windows_toast(title: str, message: str) -> None:
    """Windowsトースト通知を非同期で送信する（標準ライブラリのみで完結）。"""
    if sys.platform != "win32":
        return

    def _worker():
        try:
            def escape_xml(s: str) -> str:
                return (
                    s.replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                    .replace('"', "&quot;")
                    .replace("'", "&apos;")
                )

            safe_title = escape_xml(title)
            safe_msg = escape_xml(message)

            ps_script = f"""
$ProgressPreference = 'SilentlyContinue'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text>{safe_title}</text><text>{safe_msg}</text></binding></visual></toast>')
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe').Show($toast)
"""
            encoded = base64.b64encode(ps_script.encode("utf-16le")).decode("ascii")
            subprocess.run(
                ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-EncodedCommand", encoded],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=5,
            )
        except Exception:
            pass

    t = threading.Thread(target=_worker, daemon=True)
    t.start()


def find_obsidian_vault_name(start_dir: Path) -> str:
    """指定ディレクトリから親ディレクトリを遡り、.obsidian が存在するディレクトリ（Vault名）を特定する。"""
    current = start_dir.resolve()
    while True:
        if (current / ".obsidian").is_dir():
            return current.name
        if current.parent == current:
            break
        current = current.parent
    return start_dir.name


def get_obsidian_window_titles() -> list[str]:
    """現在開いているObsidianのウィンドウタイトル一覧を取得する。"""
    if sys.platform != "win32":
        return []

    # 1. まず高速な EnumWindows API を試みる（通常デスクトップで瞬時に判定可能）
    titles: list[str] = []
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def enum_cb(hwnd, lparam):
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buff = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buff, length + 1)
                    val = buff.value.strip()
                    if val and "obsidian" in val.lower():
                        titles.append(val)
            return True

        cb = WNDENUMPROC(enum_cb)
        user32.EnumWindows(cb, 0)
        if titles:
            return titles
    except Exception:
        pass

    # 2. EnumWindows で見つからない場合、tasklist から取得（別セッション・サンドボックスでも捕捉可能）
    try:
        raw = subprocess.check_output(
            ["tasklist", "/v", "/fi", "IMAGENAME eq Obsidian.exe", "/fo", "csv"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for enc in ("cp932", "utf-8", "shift_jis"):
            try:
                text = raw.decode(enc)
                break
            except Exception:
                continue
        else:
            text = raw.decode("latin1", errors="replace")

        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        if len(rows) > 1:
            for row in rows[1:]:
                if len(row) >= 9:
                    title = row[8].strip()
                    if title and title not in ("N/A", "OleMainThreadWndName"):
                        titles.append(title)
        return titles
    except Exception:
        return []


def is_obsidian_vault_window_open(vault_name: str) -> bool:
    """指定されたVault名に対応するObsidianウィンドウが開いているか判定する。"""
    titles = get_obsidian_window_titles()
    for title in titles:
        # Obsidianのウィンドウタイトル形式:
        # "{ノート名} - {vault名} - Obsidian v1.x.x" または "{vault名} - Obsidian v1.x.x"
        pattern = rf"(^| - ){re.escape(vault_name)} - Obsidian"
        if re.search(pattern, title, re.IGNORECASE):
            return True
        if f"{vault_name} - Obsidian" in title:
            return True
    return False


@dataclass
class ProjectConfig:
    project_dir: Path
    config_file: Path
    output_file: Path
    template_content: str
    template_file: Path | None = None
    readonly: bool = False
    warning_banner: bool = True
    auto_rollback: bool = True


def set_file_writable(file_path: Path) -> None:
    """ファイルの書き込み権限を付与する（読み取り専用属性を解除）。"""
    if file_path.exists():
        try:
            os.chmod(file_path, stat.S_IWRITE | stat.S_IREAD)
        except Exception as e:
            print(f"[警告] ファイル書き込み権限の付与に失敗しました ({file_path}): {e}", flush=True)


def set_file_readonly(file_path: Path) -> None:
    """ファイルを読み取り専用に設定する。"""
    if file_path.exists():
        try:
            os.chmod(file_path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
        except Exception as e:
            print(f"[警告] 読み取り専用属性の設定に失敗しました ({file_path}): {e}", flush=True)


def parse_frontmatter(content: str) -> tuple[dict, str]:
    """Frontmatter (---で囲まれたYAML) と本文を分離する。"""
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            try:
                data = yaml.safe_load(parts[1]) or {}
                body = parts[2].lstrip("\r\n")
                return data, body
            except Exception as e:
                print(f"[警告] Frontmatterのパースに失敗しました: {e}", flush=True)
    return {}, content


def load_project_config(dir_path: Path) -> ProjectConfig | None:
    """指定されたディレクトリ内に定義ファイルが存在すれば読み込んで ProjectConfig を返す。"""
    # 1. report_config.yaml を探索
    for cfg_name in CONFIG_FILENAMES:
        cfg_path = dir_path / cfg_name
        if cfg_path.is_file():
            try:
                raw_text = cfg_path.read_text(encoding="utf-8")
                data = yaml.safe_load(raw_text) or {}
                output_name = data.get("output", "_完成レポート.md")
                output_file = (dir_path / output_name).resolve()

                readonly = data.get("readonly", False)
                warning_banner = data.get("warning_banner", True)
                auto_rollback = data.get("auto_rollback", True)

                template_content = data.get("template")
                template_file = None

                if template_content is None:
                    # 外部テンプレートファイルを読み込む
                    t_name = data.get("template_file")
                    if t_name:
                        t_path = dir_path / t_name
                        if t_path.is_file():
                            template_content = t_path.read_text(encoding="utf-8")
                            template_file = t_path.resolve()
                        else:
                            print(f"[警告] 指定されたテンプレートファイルが見つかりません: {t_path}", flush=True)
                            template_content = ""
                    else:
                        template_content = ""

                return ProjectConfig(
                    project_dir=dir_path.resolve(),
                    config_file=cfg_path.resolve(),
                    output_file=output_file,
                    template_content=template_content,
                    template_file=template_file,
                    readonly=bool(readonly),
                    warning_banner=bool(warning_banner),
                    auto_rollback=bool(auto_rollback),
                )
            except Exception as e:
                print(f"[エラー] 設定ファイル読み込みエラー ({cfg_path}): {e}", flush=True)
                return None

    # 2. _template.md を探索
    for t_name in TEMPLATE_FILENAMES:
        t_path = dir_path / t_name
        if t_path.is_file():
            try:
                raw_text = t_path.read_text(encoding="utf-8")
                frontmatter, body = parse_frontmatter(raw_text)
                output_name = frontmatter.get("output", "_完成レポート.md")
                output_file = (dir_path / output_name).resolve()

                readonly = frontmatter.get("readonly", False)
                warning_banner = frontmatter.get("warning_banner", True)
                auto_rollback = frontmatter.get("auto_rollback", True)

                return ProjectConfig(
                    project_dir=dir_path.resolve(),
                    config_file=t_path.resolve(),
                    output_file=output_file,
                    template_content=body,
                    template_file=t_path.resolve(),
                    readonly=bool(readonly),
                    warning_banner=bool(warning_banner),
                    auto_rollback=bool(auto_rollback),
                )
            except Exception as e:
                print(f"[エラー] テンプレートファイル読み込みエラー ({t_path}): {e}", flush=True)
                return None

    return None


def find_project_config_for_file(file_path: Path, root_dir: Path = WATCH_ROOT) -> ProjectConfig | None:
    """変更されたファイルの親ディレクトリから遡って最も近い定義ファイルを探索する。"""
    curr = file_path.resolve().parent
    root_resolved = root_dir.resolve()

    while True:
        config = load_project_config(curr)
        if config is not None:
            return config

        if curr == root_resolved or curr == curr.parent:
            break
        curr = curr.parent

    return None


def find_all_projects(root_dir: Path = WATCH_ROOT) -> list[ProjectConfig]:
    """root_dir配下のすべてのプロジェクト定義を収集する。"""
    projects = []
    root_res = root_dir.resolve()
    dirs_to_check = [root_res] + [p for p in root_res.glob("**/") if p.is_dir()]
    
    seen_dirs = set()
    for d in dirs_to_check:
        d_res = d.resolve()
        if d_res in seen_dirs:
            continue
        # 隠しディレクトリ（.obsidianなど）はスキップ
        try:
            rel = d_res.relative_to(root_res)
            if any(part.startswith(".") for part in rel.parts):
                continue
        except ValueError:
            pass

        cfg = load_project_config(d_res)
        if cfg:
            projects.append(cfg)
            seen_dirs.add(d_res)

    return projects


def create_default_template_if_needed(dir_path: Path, root_dir: Path = WATCH_ROOT) -> Path | None:
    """
    指定ディレクトリが監視ルート直下の新規プロジェクトの場合に、初期テンプレート(_template.md)を自動生成する。
    - 直下以外のサブディレクトリ（2階層目以降）は対象外
    - 隠しディレクトリ（.obsidianなど）やキャッシュディレクトリ（__pycache__）は除外
    - エクスプローラー新規作成時の一時フォルダー名（「新しいフォルダー」等）はリネーム確定まで保留
    - 既存の設定ファイルやテンプレートファイルが存在する場合は上書き防止のためスキップ
    """
    try:
        resolved_dir = dir_path.resolve()
        resolved_root = root_dir.resolve()
    except Exception:
        return None

    # 1. ディレクトリの存在確認
    if not resolved_dir.is_dir():
        return None

    # 2. 監視ルートの直下（第1階層）であるかを判定（親ディレクトリがWATCH_ROOTと一致）
    if resolved_dir.parent != resolved_root:
        return None

    # 3. 隠しフォルダやキャッシュフォルダは除外
    dir_name = resolved_dir.name
    if dir_name.startswith(".") or dir_name in ("__pycache__",):
        return None

    # 4. Windowsエクスプローラーの一時的なフォルダー名（新しいフォルダー等）はリネームまで保留
    if TEMP_DIR_PATTERN.match(dir_name):
        return None

    # 5. 上書き防止: 既存の定義ファイルまたはテンプレートファイルが存在するか確認
    existing_config = load_project_config(resolved_dir)
    if existing_config is not None:
        return None

    # 念のため _template.md 自身の存在もチェック
    template_path = resolved_dir / "_template.md"
    if template_path.exists():
        return None

    # 6. _template.md を生成
    content = DEFAULT_TEMPLATE_CONTENT.replace("{title}", dir_name)
    try:
        template_path.write_text(content, encoding="utf-8")
        print(
            f"[{time.strftime('%H:%M:%S')}] [{dir_name}] "
            f"初期テンプレートを自動生成しました -> {template_path.name}",
            flush=True,
        )
        # 初期レポートの自動生成
        cfg = load_project_config(resolved_dir)
        if cfg:
            generate_report(cfg)
        return template_path
    except Exception as e:
        print(f"[エラー] テンプレート自動生成に失敗しました ({template_path}): {e}", flush=True)
        return None


def clean_block_text(text: str) -> str:
    """
    コードブロックから抽出した本文の前後の余分な空行・空白を除去する。
    - 先頭の空行（改行のみ、または空白のみの行）を除去。
    - ただし、本文先頭行の行頭空白（全角スペースや字下げインデント）は保持する。
    - 末尾の余分な空白・改行を除去する。
    """
    if not text:
        return ""
    # 先頭の空行を除去（本文最初の行頭にある全角スペースやインデントは残す）
    cleaned = re.sub(r"^(?:[ \t\u3000]*\r?\n)+", "", text)
    # 末尾の空白・改行を除去
    return cleaned.rstrip()


def extract_blocks(project_dir: Path, exclude_paths: set[Path]) -> dict[str, str]:
    """プロジェクトディレクトリ配下の全マークダウンファイルからブロックを抽出する。"""
    blocks: dict[str, str] = {}

    for md_file in project_dir.glob("**/*.md"):
        md_res = md_file.resolve()
        if md_res in exclude_paths:
            continue

        # 隠しフォルダ内のファイルは除外
        try:
            rel = md_res.relative_to(project_dir)
            if any(part.startswith(".") for part in rel.parts):
                continue
        except ValueError:
            pass

        try:
            content = md_res.read_text(encoding="utf-8")
            for tag, text in BLOCK_PATTERN.findall(content):
                tag_clean = tag.strip()
                if tag_clean in blocks:
                    print(f"  [注意] タグ '{tag_clean}' が重複しています ({md_file.name} で上書き)", flush=True)
                blocks[tag_clean] = clean_block_text(text)
        except Exception as e:
            print(f"  [警告] ファイル読み込みスキップ ({md_file.name}): {e}", flush=True)
            continue

    return blocks


def calculate_character_counts(text: str) -> tuple[int, int]:
    """
    テキストの文字数を集計する。
    - コメントタグ（<!-- ... -->）および未作成・未記入ラベル（【未作成: ...】, 【未記入: ...】）は集計から除外。
    戻り値: (空白除外文字数, 空白含む文字数)
    """
    # HTMLコメントタグを除去
    no_comments = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    # 未作成・未記入ラベルを除去
    no_labels = re.sub(r"【(?:未作成|未記入):[^】\r\n]*?】", "", no_comments)
    # 改行コードを正規化
    normalized = no_labels.replace("\r\n", "\n")

    # 空白・改行をすべて除外した純文字数
    no_spaces = re.sub(r"\s+", "", normalized)
    count_no_spaces = len(no_spaces)

    # 改行のみ除外した文字数（空白を含む）
    with_spaces = normalized.replace("\n", "")
    count_with_spaces = len(with_spaces)

    return count_no_spaces, count_with_spaces


def build_report_content(config: ProjectConfig) -> tuple[str, int, int] | None:
    """テンプレートと下書きブロックから完成レポートの内容と文字数を生成する。"""
    exclude_paths = {config.output_file.resolve(), config.config_file.resolve()}
    if config.template_file:
        exclude_paths.add(config.template_file.resolve())

    # 1. 下書きブロック収集
    blocks = extract_blocks(config.project_dir, exclude_paths)

    template = config.template_content
    if not template:
        return None

    # 2. HTMLコメント形式のプレースホルダー置換
    # <!-- INSERT:tag --> または <!-- INSERT:tag -->...<!-- END:tag -->
    def replace_comment(match: re.Match) -> str:
        tag = match.group(1).strip()
        # 文字数カウントタグ等は後で処理するためスキップ
        if tag.lower() in ("char_count", "char_count_with_spaces", "char_count_raw"):
            return match.group(0)

        if tag not in blocks:
            return f"<!-- INSERT:{tag} -->\n【未作成: {tag}】\n<!-- END:{tag} -->"
        elif not blocks[tag].strip():
            return f"<!-- INSERT:{tag} -->\n【未記入: {tag}】\n<!-- END:{tag} -->"
        else:
            body = blocks[tag]
            return f"<!-- INSERT:{tag} -->\n{body}\n<!-- END:{tag} -->"

    merged = COMMENT_PLACEHOLDER_PATTERN.sub(replace_comment, template)

    # 3. Mustache形式のプレースホルダー置換
    # {{tag}}
    def replace_mustache(match: re.Match) -> str:
        tag = match.group(1).strip()
        if tag in ("char_count", "char_count_with_spaces", "char_count_raw"):
            return match.group(0)

        if tag not in blocks:
            return f"【未作成: {tag}】"
        elif not blocks[tag].strip():
            return f"【未記入: {tag}】"
        else:
            return blocks[tag]

    merged = MUSTACHE_PLACEHOLDER_PATTERN.sub(replace_mustache, merged)

    # 4. 文字数カウント集計と置換
    count_no_spaces, count_with_spaces = calculate_character_counts(merged)

    # 特殊タグ置換
    merged = re.sub(r"\{\{char_count\}\}", f"{count_no_spaces:,}", merged)
    merged = re.sub(r"\{\{char_count_with_spaces\}\}", f"{count_with_spaces:,}", merged)
    merged = re.sub(r"\{\{char_count_raw\}\}", str(count_no_spaces), merged)

    merged = re.sub(r"<!--\s*INSERT:\s*char_count\s*-->(?:.*?<!--\s*END:\s*char_count\s*-->)?", f"{count_no_spaces:,}", merged, flags=re.DOTALL)
    merged = re.sub(r"<!--\s*CHAR_COUNT\s*-->", f"{count_no_spaces:,}", merged)
    merged = re.sub(r"<!--\s*CHAR_COUNT_WITH_SPACES\s*-->", f"{count_with_spaces:,}", merged)

    # 5. 誤編集防止警告バナーの挿入（文字数カウント集計の後に付与し、集計対象から除外）
    if config.warning_banner:
        merged = WARNING_BANNER + merged

    return merged, count_no_spaces, count_with_spaces


def generate_report(config: ProjectConfig) -> bool:
    """テンプレートと下書きブロックをマージして完成レポートを出力する。"""
    res = build_report_content(config)
    if res is None:
        print(f"[{time.strftime('%H:%M:%S')}] テンプレートが空のため出力をスキップします: {config.project_dir.name}", flush=True)
        return False

    merged, count_no_spaces, count_with_spaces = res

    # 既存ファイルとの差分チェック & 書き込み
    need_write = True
    if config.output_file.exists():
        try:
            existing = config.output_file.read_text(encoding="utf-8")
            if existing == merged:
                need_write = False
        except Exception:
            pass

    if need_write:
        try:
            config.output_file.parent.mkdir(parents=True, exist_ok=True)
            # 既存の読み取り専用属性を一時解除して書き込み
            set_file_writable(config.output_file)
            config.output_file.write_text(merged, encoding="utf-8")

            # 読み取り専用属性の同期
            if config.readonly:
                set_file_readonly(config.output_file)
            else:
                set_file_writable(config.output_file)

            print(
                f"[{time.strftime('%H:%M:%S')}] [{config.project_dir.name}] "
                f"更新完了 -> {config.output_file.name} "
                f"(文字数: {count_no_spaces:,}字 / 空白込: {count_with_spaces:,}字)",
                flush=True
            )
            return True
        except Exception as e:
            if config.readonly:
                set_file_readonly(config.output_file)
            print(f"[エラー] ファイル書き込み失敗 ({config.output_file}): {e}", flush=True)
            return False
    else:
        # 差分がなくても、設定に合わせて読み取り専用属性を同期
        if config.readonly:
            set_file_readonly(config.output_file)
        else:
            set_file_writable(config.output_file)
        return False


def rollback_report(config: ProjectConfig) -> bool:
    """完成レポートが直接変更された場合に、下書きとテンプレートから自動復元（ロールバック）する。"""
    if not config.output_file.exists():
        return False

    res = build_report_content(config)
    if res is None:
        return False

    merged, count_no_spaces, count_with_spaces = res

    try:
        existing = config.output_file.read_text(encoding="utf-8")
    except Exception:
        existing = None

    # 内容が異なる場合のみロールバックを実行
    if existing != merged:
        try:
            set_file_writable(config.output_file)
            config.output_file.write_text(merged, encoding="utf-8")
            if config.readonly:
                set_file_readonly(config.output_file)
            else:
                set_file_writable(config.output_file)

            print(
                f"[{time.strftime('%H:%M:%S')}] [{config.project_dir.name}] "
                f"直接編集を検知しました。下書きから自動復元（ロールバック）しました -> {config.output_file.name}",
                flush=True
            )
            return True
        except Exception as e:
            print(f"[エラー] 自動復元（ロールバック）に失敗しました ({config.output_file}): {e}", flush=True)
            return False
    else:
        # 差分がない場合も属性を同期
        if config.readonly:
            set_file_readonly(config.output_file)
        else:
            set_file_writable(config.output_file)
        return False


class ReportChangeHandler(FileSystemEventHandler):
    """ファイル変更イベントを監視し、対応するプロジェクトのレポートを更新する。"""

    def __init__(self):
        super().__init__()
        self._last_modified_time: dict[str, float] = {}

    def _debounce(self, path_str: str, interval: float = 0.3) -> bool:
        """短時間の同一ファイルイベントをスキップする。"""
        now = time.time()
        last = self._last_modified_time.get(path_str, 0)
        if now - last < interval:
            return False
        self._last_modified_time[path_str] = now
        return True

    def _handle_event(self, src_path: str):
        path = Path(src_path).resolve()
        if not path.is_file():
            return

        # 隠しファイルや .obsidian などは除外
        try:
            rel = path.relative_to(WATCH_ROOT)
            if any(part.startswith(".") for part in rel.parts):
                return
        except ValueError:
            return

        # 監視対象拡張子: .md, .yaml, .yml
        if path.suffix.lower() not in (".md", ".yaml", ".yml"):
            return

        # デバウンス判定
        if not self._debounce(str(path)):
            return

        # 対応するプロジェクト定義を探索
        config = find_project_config_for_file(path)
        if config is None:
            return

        # 出力ファイル自身の変更イベントの場合（直接編集検知と自動復元）
        if path.resolve() == config.output_file.resolve():
            if config.auto_rollback:
                rollback_report(config)
            return

        # レポートを更新
        generate_report(config)

    def on_modified(self, event):
        if not event.is_directory:
            self._handle_event(event.src_path)

    def on_created(self, event):
        if event.is_directory:
            create_default_template_if_needed(Path(event.src_path))
        else:
            self._handle_event(event.src_path)

    def on_moved(self, event):
        if event.is_directory:
            create_default_template_if_needed(Path(event.dest_path))
        else:
            self._handle_event(event.dest_path)

    def on_deleted(self, event):
        if not event.is_directory:
            # ファイル削除時は親ディレクトリを元に探索
            p = Path(event.src_path).resolve()
            config = find_project_config_for_file(p)
            if config:
                generate_report(config)


def main():
    print("=" * 60, flush=True)
    print("レポート自動集約・リアルタイム生成ツール (gather-md)", flush=True)
    print(f"監視ルート: {WATCH_ROOT}", flush=True)
    print("=" * 60, flush=True)

    # 1. 二重起動防止チェック
    instance_lock = SingleInstance(WATCH_ROOT)
    if not instance_lock.acquire():
        send_windows_toast("gather-md 警告", f"既に起動しているため終了しました ({WATCH_ROOT.name})")
        print(f"\n[エラー] 既に同じディレクトリで gather-md が起動しています: {WATCH_ROOT}", flush=True)
        sys.exit(1)

    # 2. Obsidian 書庫（Vault）の特定と連動待機状態の初期化
    vault_name = find_obsidian_vault_name(WATCH_ROOT)
    is_open = is_obsidian_vault_window_open(vault_name)
    attached = is_open

    # 起動通知（案B: 書庫が開いていない場合も通知）
    if attached:
        send_windows_toast(
            "gather-md 起動",
            f"監視を開始しました ({WATCH_ROOT.name}) / Obsidian ({vault_name}) と連動中",
        )
        print(f"[情報] 対象のObsidian書庫 ({vault_name}) を検出しました。連動を開始します。", flush=True)
    else:
        send_windows_toast(
            "gather-md 起動",
            f"監視を開始しました ({WATCH_ROOT.name})。Obsidian ({vault_name}) の起動を待機中",
        )
        print(f"[情報] 対象のObsidian書庫 ({vault_name}) は現在開かれていません。起動を待機します...", flush=True)

    # 3. 起動時に配下の全プロジェクトを検出して初回ビルドを実行
    projects = find_all_projects()
    if projects:
        print(f"\n{len(projects)} 件のプロジェクト定義を検出しました:", flush=True)
        for p in projects:
            print(f"  - [{p.project_dir.name}]", flush=True)
            print(f"      定義: {p.config_file.name}", flush=True)
            print(f"      出力: {p.output_file.name}", flush=True)
            generate_report(p)
    else:
        print("\n[注意] 定義ファイル (report_config.yaml または _template.md) が見つかりませんでした。", flush=True)
        print("  対象ディレクトリに定義ファイルを配置すると、自動的に監視・生成が有効になります。", flush=True)

    print("\n監視を開始しました (Ctrl+C で終了)...", flush=True)

    event_handler = ReportChangeHandler()
    observer = Observer()
    observer.schedule(event_handler, path=str(WATCH_ROOT), recursive=True)
    observer.start()

    disappear_count = 0
    obsidian_check_counter = 0

    try:
        while True:
            time.sleep(1)
            obsidian_check_counter += 1

            # 約2秒ごとに対象Obsidian書庫のウィンドウ状態をチェック
            if obsidian_check_counter >= 2:
                obsidian_check_counter = 0
                is_currently_open = is_obsidian_vault_window_open(vault_name)

                if not attached:
                    # 待機中にObsidian（対象書庫）が開かれた場合
                    if is_currently_open:
                        attached = True
                        disappear_count = 0
                        send_windows_toast(
                            "gather-md 連動開始",
                            f"Obsidian ({vault_name}) を検出しました。終了監視を開始します。",
                        )
                        print(
                            f"[{time.strftime('%H:%M:%S')}] [情報] Obsidian ({vault_name}) の起動を検知しました。終了連動を有効化します。",
                            flush=True,
                        )
                else:
                    # 連動中に対象書庫が閉じられたか判定
                    if not is_currently_open:
                        disappear_count += 1
                        # 一時的なリロード等による誤検知を防ぐため連続2回（約4秒）未検出で終了
                        if disappear_count >= 2:
                            send_windows_toast(
                                "gather-md 停止",
                                f"Obsidian ({vault_name}) の終了を検知したため停止しました。",
                            )
                            print(
                                f"\n[{time.strftime('%H:%M:%S')}] [情報] Obsidian ({vault_name}) の終了を検知しました。プログラムを自動終了します。",
                                flush=True,
                            )
                            break
                    else:
                        disappear_count = 0

    except KeyboardInterrupt:
        print("\n監視を停止しています...", flush=True)
        send_windows_toast("gather-md 停止", f"監視を終了しました ({WATCH_ROOT.name})。")
    except Exception as e:
        print(f"\n[致命的エラー] {e}", file=sys.stderr, flush=True)
        send_windows_toast("gather-md エラー", f"予期せぬエラーにより停止しました: {e}")
    finally:
        observer.stop()
        observer.join()
        instance_lock.release()
        print("終了しました。", flush=True)


if __name__ == "__main__":
    main()