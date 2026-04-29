from __future__ import annotations

import asyncio
import json
import locale
import re
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
    pass


@dataclass(slots=True)
class StrategyEntry:
    keywords: list[str]
    last_updated: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "keywords": self.keywords,
            "last_updated": self.last_updated,
        }


@dataclass(slots=True)
class MatchResult:
    image_name: str
    image_path: Path
    matched_keyword: str
    display_keyword: str
    last_updated: str | None


def parse_timestamp(value: str | None) -> datetime | None:
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
    parsed = parse_timestamp(value)
    if parsed is None:
        return None
    return parsed.isoformat()


def display_timestamp(value: str | None) -> str:
    parsed = parse_timestamp(value)
    if parsed is None:
        return "未记录"
    return parsed.strftime("%Y-%m-%d")


def normalize_keywords(keywords: Iterable[str]) -> list[str]:
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
    segments = re.split(r"[,，]", raw_keywords)
    return normalize_keywords(segments)


def _image_priority(path: Path) -> tuple[int, str]:
    suffix_order = {
        ".png": 0,
        ".jpg": 1,
        ".jpeg": 2,
        ".gif": 3,
        ".webp": 4,
    }
    return suffix_order.get(path.suffix.lower(), 99), path.name.lower()


class StrategyStore:
    def __init__(self, storage_path: Path, template_path: Path):
        self.storage_path = storage_path
        self.template_path = template_path
        self.entries: dict[str, StrategyEntry] = {}

    def load(self) -> None:
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
                entry, entry_changed = self._normalize_entry(template_store.get(name, {}))
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
        if not isinstance(raw_entry, dict):
            return StrategyEntry([], None), True

        raw_keywords = raw_entry.get("keywords", [])
        keywords = normalize_keywords(raw_keywords if isinstance(raw_keywords, list) else [])
        raw_timestamp = raw_entry.get("last_updated")
        last_updated = normalize_timestamp(raw_timestamp if isinstance(raw_timestamp, str) else None)

        normalized = StrategyEntry(keywords, last_updated)
        changed = normalized.to_dict() != {
            "keywords": raw_entry.get("keywords"),
            "last_updated": raw_entry.get("last_updated"),
        }
        return normalized, changed

    def save(self) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            name: entry.to_dict()
            for name, entry in self.entries.items()
        }
        self.storage_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def ensure_entry(self, image_name: str) -> StrategyEntry:
        if image_name not in self.entries:
            self.entries[image_name] = StrategyEntry([], None)
        return self.entries[image_name]

    def add_keywords(self, image_name: str, keywords: Iterable[str]) -> StrategyEntry:
        entry = self.ensure_entry(image_name)
        merged = normalize_keywords([*entry.keywords, *keywords])
        entry.keywords = merged
        self.save()
        return entry

    def remove_entry(self, image_name: str) -> bool:
        removed = self.entries.pop(image_name, None) is not None
        if removed:
            self.save()
        return removed

    def update_timestamp(self, image_name: str, timestamp: str | None) -> bool:
        normalized = normalize_timestamp(timestamp)
        entry = self.ensure_entry(image_name)
        if entry.last_updated == normalized:
            return False
        entry.last_updated = normalized
        return True

    def has_keyword(self, keyword: str) -> bool:
        for entry in self.entries.values():
            if keyword in entry.keywords:
                return True
        return False

    def find_keyword_matches(self, keyword: str) -> list[str]:
        return [
            image_name
            for image_name, entry in self.entries.items()
            if keyword in entry.keywords
        ]

    def display_keyword(self, image_name: str) -> str:
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
        candidates = list(image_names)
        if not candidates:
            return None

        ranked_candidates: list[tuple[datetime, int, str]] = []
        for index, image_name in enumerate(candidates):
            parsed = parse_timestamp(self.entries.get(image_name, StrategyEntry([], None)).last_updated)
            if require_timestamp and parsed is None:
                continue
            ranked_candidates.append((parsed or _EMPTY_UTC, -index, image_name))

        if not ranked_candidates:
            return None

        return max(ranked_candidates)[2]

    def format_entries(self) -> str:
        lines: list[str] = []
        for image_name, entry in self.entries.items():
            keywords = "、".join(entry.keywords) if entry.keywords else "(无关键词)"
            lines.append(
                f"{image_name}: {keywords} | 更新时间: {display_timestamp(entry.last_updated)}"
            )
        return "\n".join(lines) if lines else "当前没有任何乐土攻略配置。"

    def format_entry_blocks(self) -> list[str]:
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
    def __init__(
        self,
        *,
        storage_dir: Path,
        repo_directory_name: str,
        repository_url: str,
        template_strategies_path: Path,
    ):
        self.storage_dir = storage_dir
        self.repo_path = storage_dir / repo_directory_name
        self.repository_url = repository_url
        self.store = StrategyStore(storage_dir / INDEX_FILE_NAME, template_strategies_path)
        self._image_index: dict[str, Path] = {}

    def load(self) -> None:
        self.store.load()
        self.scan_images()
        self.sync_discovered_images()

    def is_git_repository(self) -> bool:
        return (self.repo_path / ".git").exists()

    def has_keyword(self, keyword: str) -> bool:
        return self.store.has_keyword(keyword)

    def scan_images(self) -> dict[str, Path]:
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
        if self.is_git_repository():
            self.scan_images()
            self.sync_discovered_images()
            await self.refresh_timestamps(overwrite=False)
            return {
                "already_exists": True,
                "image_count": len(self._image_index),
            }

        if self.repo_path.exists() and any(self.repo_path.iterdir()):
            raise GitCommandError(
                f"目标目录已存在且非空: {self.repo_path}，请先清理该目录后重试。"
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
            raise GitCommandError(stderr or stdout or "git clone 执行失败。")

        self.scan_images()
        self.sync_discovered_images()
        await self.refresh_timestamps(overwrite=True)
        return {
            "already_exists": False,
            "image_count": len(self._image_index),
            "stdout": stdout,
            "stderr": stderr,
        }

    async def update_repository(self) -> dict[str, object]:
        if not self.is_git_repository():
             raise GitCommandError("未找到本地攻略仓库，请先执行:\n/获取乐土攻略")

        old_commit = await self.get_head_commit()
        await self.validate_commit_hash(old_commit)

        returncode, stdout, stderr = await self.run_git(
            "pull",
            "--no-rebase",
            cwd=self.repo_path,
        )
        if returncode != 0:
            raise GitCommandError(stderr or stdout or "git pull 执行失败。")

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
        returncode, stdout, stderr = await self.run_git("rev-parse", "HEAD", cwd=self.repo_path)
        if returncode != 0:
            raise GitCommandError(stderr or stdout or "无法读取当前仓库 HEAD。")
        return stdout.splitlines()[-1].strip()

    async def validate_commit_hash(self, commit_hash: str) -> None:
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

    async def get_changed_image_names(self, old_commit: str, new_commit: str) -> list[str]:
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
            raise GitCommandError(stderr or stdout or "无法获取更新后的文件差异。")

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
            raise GitCommandError(stderr or stdout or f"无法读取文件的提交时间: {relative_path}")
        if not stdout.strip():
            return None
        return normalize_timestamp(stdout.splitlines()[-1].strip())

    async def run_git(self, *args: str, cwd: Path) -> tuple[int, str, str]:
        try:
            process = await asyncio.create_subprocess_exec(
                "git",
                *args,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise GitCommandError("当前环境未找到 git，请先安装并加入 PATH。") from exc

        stdout_bytes, stderr_bytes = await process.communicate()
        stdout = stdout_bytes.decode(_ENCODING, errors="ignore").strip()
        stderr = stderr_bytes.decode(_ENCODING, errors="ignore").strip()
        return process.returncode, stdout, stderr