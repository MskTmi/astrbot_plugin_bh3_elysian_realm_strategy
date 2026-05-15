from __future__ import annotations

import asyncio
import json
import locale
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
import urllib.request
import urllib.error

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
    # 保存单个资源在运行时合并后的关键词与更新时间
    keywords: list[str]
    last_updated: str | None = None

    def to_dict(self) -> dict[str, object]:
        # 将数据条目转换为可序列化字典
        return {
            "keywords": self.keywords,
            "last_updated": self.last_updated,
        }


@dataclass(slots=True)
class ResourceEntry:
    # 保存单个攻略资源的图片路径与更新时间
    image: str
    last_updated: str | None = None

    def to_dict(self) -> dict[str, object]:
        # 将资源信息转换为可序列化字典
        return {
            "image": self.image,
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
    # 负责默认索引、本地 overlay 和运行时合并索引
    def __init__(
        self,
        *,
        local_index_path: Path,
        git_repo_index_path: Path | None = None,
        legacy_index_path: Path | None = None,
    ):
        # 初始化本地 overlay、git 仓库中的索引路径和兼容旧版索引路径
        self.local_index_path = local_index_path
        self.git_repo_index_path = git_repo_index_path
        self.legacy_index_path = legacy_index_path
        self.official_resources: dict[str, ResourceEntry] = {}
        self.official_keywords: dict[str, list[str]] = {}
        self.local_resources: dict[str, ResourceEntry] = {}
        self.local_keywords: dict[str, list[str]] = {}
        self.resources: dict[str, ResourceEntry] = {}
        self.keywords: dict[str, list[str]] = {}
        self.entries: dict[str, StrategyEntry] = {}

    def load(self) -> None:
        # 加载默认索引和本地 overlay，并构建运行时合并索引
        if self.git_repo_index_path and self.git_repo_index_path.exists():
            # 从本地 git 仓库中的 dist/elysian-realm-index.json 读取
            (
                self.official_resources,
                self.official_keywords,
            ) = self._load_runtime_index(self.git_repo_index_path)
        
        self.local_resources, self.local_keywords = self._load_runtime_index(
            self.local_index_path,
            rename_broken=True,
        )

        if self.legacy_index_path and not self.local_index_path.exists() and self.legacy_index_path.exists():
            if self._migrate_legacy_index():
                self.save_local()

        self._rebuild_runtime_entries()

    def _load_runtime_index(
        self,
        source_path: Path,
        *,
        rename_broken: bool = False,
    ) -> tuple[dict[str, ResourceEntry], dict[str, list[str]]]:
        # 读取新版索引文件，必要时重命名损坏文件
        if not source_path.exists():
            return {}, {}

        try:
            raw = json.loads(source_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            if rename_broken:
                broken_path = source_path.with_name(
                    f"{source_path.stem}.broken{source_path.suffix}"
                )
                source_path.replace(broken_path)
            return {}, {}

        if not isinstance(raw, dict):
            return {}, {}

        resources = self._normalize_resources(raw.get("resources", {}))
        keywords = self._normalize_keyword_map(raw.get("keywords", {}))
        return resources, keywords

    def _normalize_resources(self, raw_resources: object) -> dict[str, ResourceEntry]:
        # 将资源表规范化为资源字典
        if not isinstance(raw_resources, dict):
            return {}

        normalized_resources: dict[str, ResourceEntry] = {}
        for resource_id, raw_resource in raw_resources.items():
            if not isinstance(resource_id, str):
                continue
            normalized_id = resource_id.strip()
            if not normalized_id or not isinstance(raw_resource, dict):
                continue

            raw_image = raw_resource.get("image")
            if not isinstance(raw_image, str) or not raw_image.strip():
                continue

            raw_timestamp = raw_resource.get("last_updated")
            normalized_resources[normalized_id] = ResourceEntry(
                image=raw_image.strip(),
                last_updated=normalize_timestamp(
                    raw_timestamp if isinstance(raw_timestamp, str) else None
                ),
            )

        return normalized_resources

    def _normalize_keyword_map(self, raw_keywords: object) -> dict[str, list[str]]:
        # 将关键词倒排索引规范化为 keyword -> id[]
        if not isinstance(raw_keywords, dict):
            return {}

        normalized_keywords: dict[str, list[str]] = {}
        for keyword, raw_ids in raw_keywords.items():
            if not isinstance(keyword, str):
                continue

            normalized_keyword = keyword.strip()
            if not normalized_keyword or not isinstance(raw_ids, list):
                continue

            seen_ids: set[str] = set()
            normalized_ids: list[str] = []
            for raw_id in raw_ids:
                if not isinstance(raw_id, str):
                    continue
                normalized_id = raw_id.strip()
                if not normalized_id or normalized_id in seen_ids:
                    continue
                seen_ids.add(normalized_id)
                normalized_ids.append(normalized_id)

            if normalized_ids:
                normalized_keywords[normalized_keyword] = normalized_ids

        return normalized_keywords

    def _load_legacy_entries(self) -> dict[str, StrategyEntry]:
        # 读取旧版平铺索引，兼容历史运行时数据
        if not self.legacy_index_path.exists():
            return {}

        try:
            raw = json.loads(self.legacy_index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

        if not isinstance(raw, dict):
            return {}

        legacy_entries: dict[str, StrategyEntry] = {}
        for resource_id, raw_entry in raw.items():
            if not isinstance(resource_id, str) or not isinstance(raw_entry, dict):
                continue

            normalized_id = resource_id.strip()
            if not normalized_id:
                continue

            raw_keywords = raw_entry.get("keywords", [])
            raw_timestamp = raw_entry.get("last_updated")
            legacy_entries[normalized_id] = StrategyEntry(
                keywords=normalize_keywords(
                    raw_keywords if isinstance(raw_keywords, list) else []
                ),
                last_updated=normalize_timestamp(
                    raw_timestamp if isinstance(raw_timestamp, str) else None
                ),
            )

        return legacy_entries

    def _migrate_legacy_index(self) -> bool:
        # 将旧版平铺索引迁移为本地 overlay，仅保留关键词映射
        legacy_entries = self._load_legacy_entries()
        if not legacy_entries:
            return False

        changed = False
        for resource_id, entry in legacy_entries.items():
            for keyword in entry.keywords:
                ids = self.local_keywords.setdefault(keyword, [])
                if resource_id in ids:
                    continue
                ids.append(resource_id)
                changed = True

        if not changed:
            return False

        migrated_path = self.legacy_index_path.with_name(
            f"{self.legacy_index_path.stem}.legacy-migrated{self.legacy_index_path.suffix}"
        )
        try:
            self.legacy_index_path.replace(migrated_path)
        except OSError:
            pass
        return True

    def _rebuild_runtime_entries(self) -> None:
        # 合并默认索引与本地 overlay，构建运行时查找结构
        self.resources = {
            **self.official_resources,
            **self.local_resources,
        }

        merged_keywords: dict[str, list[str]] = {}
        for source in (self.local_keywords, self.official_keywords):
            for keyword, resource_ids in source.items():
                merged_ids = merged_keywords.setdefault(keyword, [])
                for resource_id in resource_ids:
                    if resource_id not in self.resources or resource_id in merged_ids:
                        continue
                    merged_ids.append(resource_id)

        self.keywords = {
            keyword: resource_ids
            for keyword, resource_ids in merged_keywords.items()
            if resource_ids
        }

        resource_keywords: dict[str, list[str]] = {
            resource_id: [] for resource_id in self.resources
        }
        for source in (self.local_keywords, self.official_keywords):
            for keyword, resource_ids in source.items():
                for resource_id in resource_ids:
                    if resource_id not in resource_keywords:
                        continue
                    current_keywords = resource_keywords[resource_id]
                    if keyword not in current_keywords:
                        current_keywords.append(keyword)

        self.entries = {
            resource_id: StrategyEntry(
                keywords=resource_keywords.get(resource_id, []),
                last_updated=resource.last_updated,
            )
            for resource_id, resource in self.resources.items()
        }

    def save_local(self) -> None:
        # 将本地 overlay 持久化到 local-index.json
        self.local_index_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "resources": {
                resource_id: resource.to_dict()
                for resource_id, resource in self.local_resources.items()
            },
            "keywords": self.local_keywords,
        }
        self.local_index_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get_resource(self, resource_id: str) -> ResourceEntry | None:
        # 获取指定资源的合并后资源信息
        return self.resources.get(resource_id)

    def add_keywords(self, resource_id: str, keywords: Iterable[str]) -> StrategyEntry:
        # 为指定资源追加本地关键词，并保证本地结果优先匹配
        if resource_id not in self.resources:
            raise KeyError(resource_id)

        changed = False
        for keyword in normalize_keywords(keywords):
            ids = self.local_keywords.setdefault(keyword, [])
            if resource_id in ids:
                if ids[0] == resource_id:
                    continue
                ids[:] = [resource_id, *[current_id for current_id in ids if current_id != resource_id]]
                changed = True
                continue

            ids.insert(0, resource_id)
            changed = True

        if changed:
            self.save_local()
            self._rebuild_runtime_entries()

        return self.entries.get(resource_id, StrategyEntry([], None))

    def remove_entry(self, resource_id: str) -> bool:
        # 删除指定资源在本地 overlay 中的关键词映射
        changed = False
        for keyword in list(self.local_keywords):
            resource_ids = self.local_keywords[keyword]
            if resource_id not in resource_ids:
                continue

            self.local_keywords[keyword] = [
                current_id for current_id in resource_ids if current_id != resource_id
            ]
            if not self.local_keywords[keyword]:
                del self.local_keywords[keyword]
            changed = True

        if changed:
            self.save_local()
            self._rebuild_runtime_entries()

        return changed

    def update_timestamp(self, image_name: str, timestamp: str | None) -> bool:
        # 更新指定图片的最后更新时间，返回是否有变更
        normalized = normalize_timestamp(timestamp)
        entry = self.entries.setdefault(image_name, StrategyEntry([], None))
        if entry.last_updated == normalized:
            return False
        entry.last_updated = normalized
        return True

    def has_keyword(self, keyword: str) -> bool:
        # 判断当前索引中是否包含指定关键词
        return bool(self.find_keyword_matches(keyword))

    def find_keyword_matches(self, keyword: str) -> list[str]:
        # 返回所有命中指定关键词且资源存在的资源 id
        return [
            resource_id
            for resource_id in self.keywords.get(keyword, [])
            if resource_id in self.resources
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

    def snapshot_official_resources(self) -> dict[str, tuple[str, str | None]]:
        # 抓取当前官方资源快照，用于更新后比较
        return {
            resource_id: (resource.image, resource.last_updated)
            for resource_id, resource in self.official_resources.items()
        }

    def diff_official_resources(
        self,
        previous_snapshot: dict[str, tuple[str, str | None]],
    ) -> list[str]:
        # 比较前后官方资源快照，返回发生变化的资源 id
        current_snapshot = self.snapshot_official_resources()
        changed_resource_ids: list[str] = []
        seen: set[str] = set()

        for resource_id in self.official_resources:
            if previous_snapshot.get(resource_id) == current_snapshot.get(resource_id):
                continue
            changed_resource_ids.append(resource_id)
            seen.add(resource_id)

        for resource_id in previous_snapshot:
            if resource_id in seen or resource_id in current_snapshot:
                continue
            changed_resource_ids.append(resource_id)

        return changed_resource_ids


class ElysianRealmService:
    # 封装攻略仓库同步、索引维护和关键词解析逻辑
    def __init__(
        self,
        *,
        storage_dir: Path,
        repo_directory_name: str,
        repository_url: str,
    ):
        # 初始化仓库路径和索引存储
        self.storage_dir = storage_dir
        self.repo_path = storage_dir / repo_directory_name
        self.repository_url = repository_url
        self.git_repo_index_path = self.repo_path / "dist" / "elysian-realm-index.json"
        self.store = StrategyStore(
            local_index_path=storage_dir / "local-index.json",
            git_repo_index_path=self.git_repo_index_path,
            legacy_index_path=storage_dir / INDEX_FILE_NAME,
        )

    def load(self) -> None:
        # 加载默认索引和本地 overlay
        self.store.load()

    def is_git_repository(self) -> bool:
        # 判断当前仓库目录是否已初始化为 git 仓库
        return (self.repo_path / ".git").exists()

    def has_keyword(self, keyword: str) -> bool:
        # 判断索引中是否存在指定关键词
        return self.store.has_keyword(keyword)

    def resolve_resource_path(self, resource_id: str) -> Path | None:
        # 将资源中的相对图片路径解析为可发送的本地路径
        resource = self.store.get_resource(resource_id)
        if resource is None:
            return None

        image_path = Path(resource.image)
        if image_path.is_absolute():
            return image_path
        if resource_id in self.store.local_resources:
            return self.storage_dir / image_path
        return self.repo_path / image_path

    async def clone_repository(self) -> dict[str, object]:
        # 首次克隆攻略仓库并加载默认索引
        if self.is_git_repository():
            await self.ensure_remote_url()
            self.store.load()
            return {
                "already_exists": True,
                "image_count": len(self.store.resources),
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

        self.store.load()
        return {
            "already_exists": False,
            "image_count": len(self.store.resources),
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

        self.store.load()
        return {
            "image_count": len(self.store.resources),
            "stdout": stdout,
            "stderr": stderr,
        }

    async def update_repository(self) -> dict[str, object]:
        # 更新本地仓库并重新加载默认索引
        if not self.is_git_repository():
            raise GitCommandError("未找到本地攻略仓库，请先执行 /获取乐土攻略")

        await self.ensure_remote_url()
        old_commit = await self.get_head_commit()
        await self.validate_commit_hash(old_commit)

        old_official_snapshot = self.store.snapshot_official_resources()

        try:
            returncode, stdout, stderr = await self.run_git(
                "pull",
                "--no-rebase",
                cwd=self.repo_path,
            )
            if returncode != 0:
                return {
                    "already_up_to_date": False,
                    "old_commit": old_commit,
                    "new_commit": None,
                    "updated_names": [],
                    "git_error": stderr or stdout or "git pull 执行失败",
                }
            new_commit = await self.get_head_commit()
            await self.validate_commit_hash(new_commit)
        except GitCommandError as exc:
            return {
                "already_up_to_date": False,
                "old_commit": old_commit,
                "new_commit": None,
                "updated_names": [],
                "git_error": str(exc),
            }

        self.store.load()
        changed_image_names = self.store.diff_official_resources(old_official_snapshot)

        return {
            "already_up_to_date": old_commit == new_commit and not changed_image_names,
            "old_commit": old_commit,
            "new_commit": new_commit,
            "updated_names": changed_image_names,
        }

    async def resolve_keyword(self, keyword: str) -> MatchResult | None:
        # 根据关键词解析出最合适的攻略图片结果
        matched_names = self.store.find_keyword_matches(keyword)
        if not matched_names:
            return None

        available_paths: dict[str, Path] = {}
        for resource_id in matched_names:
            resource_path = self.resolve_resource_path(resource_id)
            if resource_path is None or not resource_path.is_file():
                continue
            available_paths[resource_id] = resource_path

        available_names = list(available_paths)
        if not available_names:
            return None

        chosen_name = self.store.pick_most_recent(available_names)
        if chosen_name is None:
            return None

        entry = self.store.entries.get(chosen_name, StrategyEntry([], None))
        return MatchResult(
            image_name=chosen_name,
            image_path=available_paths[chosen_name],
            matched_keyword=keyword,
            display_keyword=self.store.display_keyword(chosen_name),
            last_updated=entry.last_updated,
        )

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
