"""
gather_md.py
レポート作成支援スクリプト:
Markdownファイル内のコードブロックを抽出し、テンプレートに流し込んで完成レポートを自動生成・更新する。
"""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
import sys
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
                blocks[tag_clean] = (text or "").strip()
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

    # 起動時に配下の全プロジェクトを検出して初回ビルドを実行
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

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n監視を停止しています...", flush=True)
        observer.stop()
    observer.join()
    print("終了しました。", flush=True)


if __name__ == "__main__":
    main()