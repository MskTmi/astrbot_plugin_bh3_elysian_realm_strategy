from __future__ import annotations

import asyncio
import json
import locale
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

_COMMIT_HASH_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_EMPTY_UTC = datetime.min.replace(tzinfo=timezone.utc)
_ENCODING = locale.getpreferredencoding(False) or "utf-8"
INDEX_FILE_NAME = "elysian-realm-index.json"


class GitCommandError(RuntimeError):
    # 表示 git 相关操作失败的业务异常
    pass


@dataclass(slots=True)
class StrategyEntry:
    # 保存单张攻略图的关键词和最后更新时间
    keywords: list[str]
    last_updated: str | None = None

    def to_dict(self) -> dict[str, object]:
        # 将数据条目转换为可序列化字典
        return {
            "keywords": self.keywords,
            "last_updated": self.last_updated,
        }


@dataclass(slots=True)
class MatchResult:
    # 描述一次关键词命中的图片结果
    image_name: str
    image_path: Path
    matched_keyword: str
    display_keyword: str
    last_updated: str | None


def parse_timestamp(value: str | None) -> datetime | None:
    # 将时间字符串解析为 UTC 时区的 datetime 对象
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_timestamp(value: str | None) -> str | None:
    # 将时间值标准化为 ISO 格式字符串
    parsed = parse_timestamp(value)
    if parsed is None:
        return None
    return parsed.isoformat()


def display_timestamp(value: str | None) -> str:
    # 将时间值格式化为面向展示的日期文本
    parsed = parse_timestamp(value)
    if parsed is None:
        return "未记录"
    return parsed.strftime("%Y-%m-%d")


def normalize_keywords(keywords: Iterable[str]) -> list[str]:
    # 清洗关键词列表并去重，保留原始顺序
    unique_keywords: list[str] = []
    seen: set[str] = set()
    for keyword in keywords:
        if not isinstance(keyword, str):
            continue
        normalized = keyword.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique_keywords.append(normalized)
    return unique_keywords


def split_keywords(raw_keywords: str) -> list[str]:
    # 按中英文逗号切分关键词字符串
    segments = re.split(r"[,，]", raw_keywords)
    return normalize_keywords(segments)


def _image_priority(path: Path) -> tuple[int, str]:
    # 为不同扩展名的图片建立稳定排序优先级
    suffix_order = {
        ".png": 0,
        ".jpg": 1,
        ".jpeg": 2,
        ".gif": 3,
        ".webp": 4,
    }
    return suffix_order.get(path.suffix.lower(), 99), path.name.lower()


class StrategyStore:
    # 负责索引文件的加载、规范化和持久化
    def __init__(self, storage_path: Path, template_path: Path):
        # 初始化运行时索引路径与模板索引路径
        self.storage_path = storage_path
        self.template_path = template_path
        self.entries: dict[str, StrategyEntry] = {}

    def load(self) -> None:
        # 合并模板索引与运行时索引，并在必要时回写规范化结果
        template_store = self._load_raw_store(self.template_path)
        runtime_store = self._load_raw_store(self.storage_path, rename_broken=True)

        ordered_names: list[str] = list(template_store)
        for name in runtime_store:
            if name not in template_store:
                ordered_names.append(name)

        normalized_entries: dict[str, StrategyEntry] = {}
        changed = not self.storage_path.exists()

        for name in ordered_names:
            if name in runtime_store:
                entry, entry_changed = self._normalize_entry(runtime_store[name])
            else:
                entry, entry_changed = self._normalize_entry(
                    template_store.get(name, {})
                )
                changed = True
            normalized_entries[name] = entry
            changed = changed or entry_changed

        self.entries = normalized_entries
        if changed:
            self.save()

    def _load_raw_store(
        self,
        source_path: Path,
        *,
        rename_broken: bool = False,
    ) -> dict[str, object]:
        # 读取原始索引文件，必要时重命名损坏文件
        if not source_path.exists():
            return {}

        try:
            raw = json.loads(source_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            if rename_broken:
                broken_path = source_path.with_name(
                    f"{source_path.stem}.broken{source_path.suffix}"
                )
                source_path.replace(broken_path)
            return {}

        if not isinstance(raw, dict):
            return {}
        return raw

    def _normalize_entry(self, raw_entry: object) -> tuple[StrategyEntry, bool]:
        # 将原始索引项规范化为 StrategyEntry，并返回是否发生变化
        if not isinstance(raw_entry, dict):
            return StrategyEntry([], None), True

        raw_keywords = raw_entry.get("keywords", [])
        keywords = normalize_keywords(
            raw_keywords if isinstance(raw_keywords, list) else []
        )
        raw_timestamp = raw_entry.get("last_updated")
        last_updated = normalize_timestamp(
            raw_timestamp if isinstance(raw_timestamp, str) else None
        )

        normalized = StrategyEntry(keywords, last_updated)
        changed = normalized.to_dict() != {
            "keywords": raw_entry.get("keywords"),
            "last_updated": raw_entry.get("last_updated"),
        }
        return normalized, changed

    def save(self) -> None:
        # 将当前索引内容持久化到运行时索引文件
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {name: entry.to_dict() for name, entry in self.entries.items()}
        self.storage_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def ensure_entry(self, image_name: str) -> StrategyEntry:
        # 确保指定图片名在索引中存在条目
        if image_name not in self.entries:
            self.entries[image_name] = StrategyEntry([], None)
        return self.entries[image_name]

    def add_keywords(self, image_name: str, keywords: Iterable[str]) -> StrategyEntry:
        # 为指定图片条目合并追加关键词并保存
        entry = self.ensure_entry(image_name)
        merged = normalize_keywords([*entry.keywords, *keywords])
        entry.keywords = merged
        self.save()
        return entry

    def remove_entry(self, image_name: str) -> bool:
        # 删除指定图片条目，并在删除成功时保存索引
        removed = self.entries.pop(image_name, None) is not None
        if removed:
            self.save()
        return removed

    def update_timestamp(self, image_name: str, timestamp: str | None) -> bool:
        # 更新指定图片的最后更新时间，返回是否有变更
        normalized = normalize_timestamp(timestamp)
        entry = self.ensure_entry(image_name)
        if entry.last_updated == normalized:
            return False
        entry.last_updated = normalized
        return True

    def has_keyword(self, keyword: str) -> bool:
        # 判断当前索引中是否包含指定关键词
        for entry in self.entries.values():
            if keyword in entry.keywords:
                return True
        return False

    def find_keyword_matches(self, keyword: str) -> list[str]:
        # 返回所有命中指定关键词的图片名
        return [
            image_name
            for image_name, entry in self.entries.items()
            if keyword in entry.keywords
        ]

    def display_keyword(self, image_name: str) -> str:
        # 返回图片条目用于展示的首选关键词
        entry = self.entries.get(image_name)
        if entry and entry.keywords:
            return entry.keywords[0]
        return image_name

    def pick_most_recent(
        self,
        image_names: Iterable[str],
        *,
        require_timestamp: bool = False,
    ) -> str | None:
        # 在多个候选图片中挑选最近更新的一张
        candidates = list(image_names)
        if not candidates:
            return None

        ranked_candidates: list[tuple[datetime, int, str]] = []
        for index, image_name in enumerate(candidates):
            parsed = parse_timestamp(
                self.entries.get(image_name, StrategyEntry([], None)).last_updated
            )
            if require_timestamp and parsed is None:
                continue
            ranked_candidates.append((parsed or _EMPTY_UTC, -index, image_name))

        if not ranked_candidates:
            return None

        return max(ranked_candidates)[2]

    def format_entries(self) -> str:
        # 将索引格式化为单个文本列表
        lines: list[str] = []
        for image_name, entry in self.entries.items():
            keywords = "、".join(entry.keywords) if entry.keywords else "(无关键词)"
            lines.append(
                f"{image_name}: {keywords} | 更新时间: {display_timestamp(entry.last_updated)}"
            )
        return "\n".join(lines) if lines else "当前没有任何乐土攻略配置"

    def format_entry_blocks(self) -> list[str]:
        # 将索引格式化为分块文本，便于转发或导出
        blocks: list[str] = []
        for image_name, entry in self.entries.items():
            block_lines = [f"{image_name}: "]
            if entry.keywords:
                block_lines.extend(f"    - {keyword}" for keyword in entry.keywords)
            else:
                block_lines.append("    - (无关键词)")
            blocks.append("\n".join(block_lines))
        return blocks


class ElysianRealmService:
    # 封装攻略仓库同步、索引维护和关键词解析逻辑
    def __init__(
        self,
        *,
        storage_dir: Path,
        repo_directory_name: str,
        repository_url: str,
        template_strategies_path: Path,
    ):
        # 初始化仓库路径、索引存储和图片缓存
        self.storage_dir = storage_dir
        self.repo_path = storage_dir / repo_directory_name
        self.repository_url = repository_url
        self.store = StrategyStore(
            storage_dir / INDEX_FILE_NAME, template_strategies_path
        )
        self._image_index: dict[str, Path] = {}

    def load(self) -> None:
        # 加载索引并同步当前仓库中的图片文件
        self.store.load()
        self.scan_images()
        self.sync_discovered_images()

    def is_git_repository(self) -> bool:
        # 判断当前仓库目录是否已初始化为 git 仓库
        return (self.repo_path / ".git").exists()

    def has_keyword(self, keyword: str) -> bool:
        # 判断索引中是否存在指定关键词
        return self.store.has_keyword(keyword)

    def scan_images(self) -> dict[str, Path]:
        # 扫描仓库中的图片文件并建立图片名到路径的索引
        image_index: dict[str, Path] = {}
        if not self.repo_path.exists():
            self._image_index = {}
            return self._image_index

        for path in self.repo_path.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            current = image_index.get(path.stem)
            if current is None or _image_priority(path) < _image_priority(current):
                image_index[path.stem] = path

        self._image_index = image_index
        return self._image_index

    def sync_discovered_images(self) -> bool:
        # 将仓库中新发现的图片同步进索引文件
        image_index = self.scan_images()
        changed = False
        for image_name in image_index:
            if image_name not in self.store.entries:
                self.store.ensure_entry(image_name)
                changed = True
        if changed:
            self.store.save()
        return changed

    async def clone_repository(self) -> dict[str, object]:
        # 首次克隆攻略仓库并初始化本地索引与时间戳
        if self.is_git_repository():
            await self.ensure_remote_url()
            self.scan_images()
            self.sync_discovered_images()
            await self.refresh_timestamps(overwrite=False)
            return {
                "already_exists": True,
                "image_count": len(self._image_index),
            }

        if self.repo_path.exists() and any(self.repo_path.iterdir()):
            raise GitCommandError(
                f"目标目录已存在且非空: {self.repo_path}，请先清理该目录后重试"
            )

        self.repo_path.parent.mkdir(parents=True, exist_ok=True)
        returncode, stdout, stderr = await self.run_git(
            "clone",
            "--depth=1",
            self.repository_url,
            str(self.repo_path),
            cwd=self.repo_path.parent,
        )
        if returncode != 0:
            raise GitCommandError(stderr or stdout or "git clone 执行失败")

        self.scan_images()
        self.sync_discovered_images()
        await self.refresh_timestamps(overwrite=True)
        return {
            "already_exists": False,
            "image_count": len(self._image_index),
            "stdout": stdout,
            "stderr": stderr,
        }

    async def force_clone_repository(self) -> dict[str, object]:
        # 删除现有攻略仓库目录后，重新浅克隆远端仓库
        if self.repo_path.exists():
            try:
                shutil.rmtree(self.repo_path)
            except OSError as exc:
                raise GitCommandError(
                    f"无法删除现有攻略仓库目录: {self.repo_path}"
                ) from exc

        self._image_index = {}
        self.repo_path.parent.mkdir(parents=True, exist_ok=True)

        returncode, stdout, stderr = await self.run_git(
            "clone",
            "--depth=1",
            self.repository_url,
            str(self.repo_path),
            cwd=self.repo_path.parent,
        )
        if returncode != 0:
            raise GitCommandError(stderr or stdout or "git clone 执行失败")

        self.scan_images()
        self.sync_discovered_images()
        await self.refresh_timestamps(overwrite=True)
        return {
            "image_count": len(self._image_index),
            "stdout": stdout,
            "stderr": stderr,
        }

    async def update_repository(self) -> dict[str, object]:
        # 更新本地攻略仓库并返回本次变更摘要
        if not self.is_git_repository():
            raise GitCommandError("未找到本地攻略仓库，请先执行:\n/获取乐土攻略")

        await self.ensure_remote_url()

        old_commit = await self.get_head_commit()
        await self.validate_commit_hash(old_commit)

        returncode, stdout, stderr = await self.run_git(
            "pull",
            "--no-rebase",
            cwd=self.repo_path,
        )
        if returncode != 0:
            raise GitCommandError(stderr or stdout or "git pull 执行失败")

        new_commit = await self.get_head_commit()
        await self.validate_commit_hash(new_commit)
        self.scan_images()
        self.sync_discovered_images()

        if old_commit == new_commit:
            return {
                "already_up_to_date": True,
                "old_commit": old_commit,
                "new_commit": new_commit,
                "updated_names": [],
                "stdout": stdout,
                "stderr": stderr,
            }

        changed_image_names = await self.get_changed_image_names(old_commit, new_commit)
        if changed_image_names:
            await self.refresh_timestamps(changed_image_names, overwrite=True)

        return {
            "already_up_to_date": False,
            "old_commit": old_commit,
            "new_commit": new_commit,
            "updated_names": changed_image_names,
            "stdout": stdout,
            "stderr": stderr,
        }

    async def resolve_keyword(self, keyword: str) -> MatchResult | None:
        # 根据关键词解析出最合适的攻略图片结果
        matched_names = self.store.find_keyword_matches(keyword)
        if not matched_names:
            return None

        image_index = self.scan_images()
        available_names = [name for name in matched_names if name in image_index]
        if not available_names:
            return None

        if len(available_names) > 1:
            await self.refresh_timestamps(available_names, overwrite=False)

        chosen_name = self.store.pick_most_recent(available_names)
        if chosen_name is None:
            return None

        entry = self.store.entries[chosen_name]
        return MatchResult(
            image_name=chosen_name,
            image_path=image_index[chosen_name],
            matched_keyword=keyword,
            display_keyword=self.store.display_keyword(chosen_name),
            last_updated=entry.last_updated,
        )

    async def refresh_timestamps(
        self,
        image_names: Iterable[str] | None = None,
        *,
        overwrite: bool,
    ) -> bool:
        # 刷新指定图片条目的最后提交时间
        if not self.is_git_repository():
            return False

        image_index = self.scan_images()
        target_names = list(image_names or image_index.keys())
        changed = False

        for image_name in target_names:
            image_path = image_index.get(image_name)
            if image_path is None:
                continue
            current_entry = self.store.ensure_entry(image_name)
            if current_entry.last_updated and not overwrite:
                continue
            relative_path = image_path.relative_to(self.repo_path).as_posix()
            timestamp = await self.get_last_commit_timestamp(relative_path)
            changed = self.store.update_timestamp(image_name, timestamp) or changed

        if changed:
            self.store.save()
        return changed

    async def get_head_commit(self) -> str:
        # 获取当前仓库 HEAD 指向的 commit 哈希
        returncode, stdout, stderr = await self.run_git(
            "rev-parse", "HEAD", cwd=self.repo_path
        )
        if returncode != 0:
            raise GitCommandError(stderr or stdout or "无法读取当前仓库 HEAD")
        return stdout.splitlines()[-1].strip()

    async def validate_commit_hash(self, commit_hash: str) -> None:
        # 校验 commit 哈希格式与对象是否真实存在
        if not _COMMIT_HASH_RE.fullmatch(commit_hash):
            raise GitCommandError(f"检测到非法 commit 哈希: {commit_hash}")

        returncode, _, stderr = await self.run_git(
            "cat-file",
            "-e",
            f"{commit_hash}^{{commit}}",
            cwd=self.repo_path,
        )
        if returncode != 0:
            raise GitCommandError(stderr or f"commit 不存在: {commit_hash}")

    async def get_changed_image_names(
        self, old_commit: str, new_commit: str
    ) -> list[str]:
        # 获取两个 commit 之间发生变化的攻略图片名列表
        await self.validate_commit_hash(old_commit)
        await self.validate_commit_hash(new_commit)

        returncode, stdout, stderr = await self.run_git(
            "diff",
            "--name-only",
            old_commit,
            new_commit,
            cwd=self.repo_path,
        )
        if returncode != 0:
            raise GitCommandError(stderr or stdout or "无法获取更新后的文件差异")

        image_index = self.scan_images()
        changed_names: list[str] = []
        seen: set[str] = set()

        for line in stdout.splitlines():
            file_path = PurePosixPath(line.strip())
            if file_path.suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            image_name = file_path.stem
            if image_name not in image_index or image_name in seen:
                continue
            seen.add(image_name)
            changed_names.append(image_name)

        return changed_names

    async def get_last_commit_timestamp(self, relative_path: str) -> str | None:
        # 获取指定文件最近一次提交的时间戳
        returncode, stdout, stderr = await self.run_git(
            "log",
            "-1",
            "--format=%cI",
            "HEAD",
            "--",
            relative_path,
            cwd=self.repo_path,
        )
        if returncode != 0:
            raise GitCommandError(
                stderr or stdout or f"无法读取文件的提交时间: {relative_path}"
            )
        if not stdout.strip():
            return None
        return normalize_timestamp(stdout.splitlines()[-1].strip())

    async def ensure_remote_url(self) -> None:
        # 确保本地仓库 origin 远端地址与当前配置一致
        returncode, stdout, stderr = await self.run_git(
            "remote",
            "get-url",
            "origin",
            cwd=self.repo_path,
        )
        if returncode != 0:
            raise GitCommandError(stderr or stdout or "无法读取当前仓库远端地址")

        current_remote_url = stdout.splitlines()[-1].strip()
        if current_remote_url == self.repository_url:
            return

        returncode, stdout, stderr = await self.run_git(
            "remote",
            "set-url",
            "origin",
            self.repository_url,
            cwd=self.repo_path,
        )
        if returncode != 0:
            raise GitCommandError(stderr or stdout or "无法更新当前仓库远端地址")

    async def run_git(self, *args: str, cwd: Path) -> tuple[int, str, str]:
        # 异步执行 git 命令并返回退出码、标准输出和标准错误
        try:
            process = await asyncio.create_subprocess_exec(
                "git",
                *args,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise GitCommandError("当前环境未找到 git，请先安装并加入 PATH") from exc

        stdout_bytes, stderr_bytes = await process.communicate()
        stdout = stdout_bytes.decode(_ENCODING, errors="ignore").strip()
        stderr = stderr_bytes.decode(_ENCODING, errors="ignore").strip()
        return process.returncode, stdout, stderr
