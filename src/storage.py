"""
存储适配器模块 - 完全基于 JSON 文件存储
整合凭证管理、状态配置持久化与平滑调度状态检索
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import aiofiles

from src.log import log


class Storage:
    """JSON 文件存储管理器"""

    # 状态字段集合
    STATE_FIELDS = {
        "error_codes",
        "error_messages",
        "disabled",
        "last_success",
        "user_email",
        "user_name",
        "model_cooldowns",
        "preview",
        "tier",
        "enable_credit",
        "quota_groups",
    }

    def __init__(self):
        self._backend = self
        self._credentials_dir: Optional[str] = None
        self._geminicli_dir: Optional[str] = None
        self._antigravity_dir: Optional[str] = None
        self._geminicli_state_file: Optional[str] = None
        self._antigravity_state_file: Optional[str] = None
        self._config_file: Optional[str] = None

        self._initialized = False
        self._lock = asyncio.Lock()

        # 内存缓存
        self._states: Dict[str, Dict[str, Dict[str, Any]]] = {
            "geminicli": {},
            "antigravity": {},
        }
        self._config_cache: Dict[str, Any] = {}

    async def initialize(self) -> None:
        """初始化存储后端"""
        if self._initialized:
            return

        async with self._lock:
            if self._initialized:
                return

            try:
                self._credentials_dir = os.getenv("CREDENTIALS_DIR", "./creds")
                self._geminicli_dir = os.path.join(self._credentials_dir, "geminicli")
                self._antigravity_dir = os.path.join(self._credentials_dir, "antigravity")

                os.makedirs(self._credentials_dir, exist_ok=True)

                self._geminicli_state_file = os.path.join(self._credentials_dir, "geminicli_state.json")
                self._antigravity_state_file = os.path.join(self._credentials_dir, "antigravity_state.json")
                self._config_file = os.path.join(self._credentials_dir, "config.json")

                # 加载现有 SQLite 数据库（如果存在）进行平滑自动迁移
                await self._migrate_from_sqlite_if_exists()

                # 加载状态文件到内存缓存
                await self._load_states("geminicli")
                await self._load_states("antigravity")

                # 执行文件名到邮箱的迁移
                await self._migrate_filenames_to_emails()

                # 加载配置到内存缓存
                await self._load_config()

                # 自动扫描并同步凭证文件目录
                await self._sync_credentials_from_disk("geminicli")
                await self._sync_credentials_from_disk("antigravity")

                self._initialized = True
                log.info(f"JSON storage initialized at {self._credentials_dir}")

            except Exception as e:
                log.error(f"Error initializing JSON storage: {e}")
                raise

    async def _migrate_from_sqlite_if_exists(self):
        """如果存在 credentials.db，自动将其中的凭证与状态迁移至 JSON 文件"""
        db_path = os.path.join(self._credentials_dir, "credentials.db")
        if not os.path.exists(db_path):
            return

        try:
            import sqlite3
            log.info("Found legacy SQLite database credentials.db, starting automatic migration to JSON...")
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            tables = [r[0] for r in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]

            if "credentials" in tables:
                cursor.execute("SELECT filename, credential_data, disabled, error_codes, error_messages, last_success, user_email, model_cooldowns, preview, tier, rotation_order FROM credentials")
                rows = cursor.fetchall()
                geminicli_state = {}
                for row in rows:
                    fname = os.path.basename(row[0])
                    try:
                        cdata = json.loads(row[1])
                        cpath = os.path.join(self._geminicli_dir, fname)
                        with open(cpath, "w", encoding="utf-8") as f:
                            json.dump(cdata, f, ensure_ascii=False, indent=2)
                    except Exception as ex:
                        log.warning(f"Failed to migrate credential file {fname}: {ex}")

                    geminicli_state[fname] = {
                        "disabled": bool(row[2]),
                        "error_codes": json.loads(row[3]) if row[3] else [],
                        "error_messages": json.loads(row[4]) if row[4] else [],
                        "last_success": row[5] or time.time(),
                        "user_email": row[6],
                        "model_cooldowns": json.loads(row[7]) if row[7] else {},
                        "preview": bool(row[8]) if row[8] is not None else True,
                        "tier": row[9] or "pro",
                        "rotation_order": row[10] or 0,
                    }

                with open(self._geminicli_state_file, "w", encoding="utf-8") as f:
                    json.dump(geminicli_state, f, ensure_ascii=False, indent=2)

            if "antigravity_credentials" in tables:
                cursor.execute("SELECT filename, credential_data, disabled, error_codes, error_messages, last_success, user_email, model_cooldowns, tier, enable_credit, rotation_order FROM antigravity_credentials")
                rows = cursor.fetchall()
                ag_state = {}
                for row in rows:
                    fname = os.path.basename(row[0])
                    try:
                        cdata = json.loads(row[1])
                        cpath = os.path.join(self._antigravity_dir, fname)
                        with open(cpath, "w", encoding="utf-8") as f:
                            json.dump(cdata, f, ensure_ascii=False, indent=2)
                    except Exception as ex:
                        log.warning(f"Failed to migrate antigravity credential file {fname}: {ex}")

                    ag_state[fname] = {
                        "disabled": bool(row[2]),
                        "error_codes": json.loads(row[3]) if row[3] else [],
                        "error_messages": json.loads(row[4]) if row[4] else [],
                        "last_success": row[5] or time.time(),
                        "user_email": row[6],
                        "model_cooldowns": json.loads(row[7]) if row[7] else {},
                        "tier": row[8] or "pro",
                        "enable_credit": bool(row[9]) if row[9] is not None else False,
                        "rotation_order": row[10] or 0,
                    }

                with open(self._antigravity_state_file, "w", encoding="utf-8") as f:
                    json.dump(ag_state, f, ensure_ascii=False, indent=2)

            if "config" in tables:
                cursor.execute("SELECT key, value FROM config")
                rows = cursor.fetchall()
                cfg_data = {}
                for k, v in rows:
                    try:
                        cfg_data[k] = json.loads(v)
                    except Exception:
                        cfg_data[k] = v

                with open(self._config_file, "w", encoding="utf-8") as f:
                    json.dump(cfg_data, f, ensure_ascii=False, indent=2)

            conn.close()

            bak_path = db_path + ".migrated.bak"
            if os.path.exists(bak_path):
                try:
                    os.remove(bak_path)
                except Exception:
                    pass
            os.rename(db_path, bak_path)
            log.info(f"SQLite migration complete! Database backed up to {bak_path}")

        except Exception as e:
            log.error(f"Error during SQLite migration: {e}")

    async def _load_states(self, mode: str) -> None:
        state_file = self._geminicli_state_file if mode == "geminicli" else self._antigravity_state_file
        if os.path.exists(state_file):
            try:
                async with aiofiles.open(state_file, "r", encoding="utf-8") as f:
                    content = await f.read()
                    self._states[mode] = json.loads(content)
            except Exception as e:
                log.error(f"Error reading state file {state_file}: {e}")
                self._states[mode] = {}
        else:
            self._states[mode] = {}

    async def _save_states(self, mode: str) -> None:
        state_file = self._geminicli_state_file if mode == "geminicli" else self._antigravity_state_file
        temp_file = state_file + ".tmp"
        try:
            data = json.dumps(self._states[mode], ensure_ascii=False, indent=2)
            async with aiofiles.open(temp_file, "w", encoding="utf-8") as f:
                await f.write(data)
            os.replace(temp_file, state_file)
        except Exception as e:
            log.error(f"Error saving state file {state_file}: {e}")
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except Exception:
                    pass

    async def _load_config(self) -> None:
        if os.path.exists(self._config_file):
            try:
                async with aiofiles.open(self._config_file, "r", encoding="utf-8") as f:
                    content = await f.read()
                    self._config_cache = json.loads(content)
                    try:
                        from src.model_mapping import model_mapping_manager
                        if "custom_map" in self._config_cache and isinstance(self._config_cache["custom_map"], dict):
                            model_mapping_manager._custom_map.update(self._config_cache["custom_map"])
                        if "fallback_map" in self._config_cache and isinstance(self._config_cache["fallback_map"], dict):
                            model_mapping_manager._fallback_map.update(self._config_cache["fallback_map"])
                    except Exception:
                        pass
            except Exception as e:
                log.error(f"Error reading config file {self._config_file}: {e}")
                self._config_cache = {}
        else:
            self._config_cache = {}

    async def _save_config(self) -> None:
        temp_file = self._config_file + ".tmp"
        try:
            try:
                from src.model_mapping import model_mapping_manager
                self._config_cache["custom_map"] = model_mapping_manager._custom_map
                self._config_cache["fallback_map"] = model_mapping_manager._fallback_map
            except Exception:
                pass

            data = json.dumps(self._config_cache, ensure_ascii=False, indent=2)
            async with aiofiles.open(temp_file, "w", encoding="utf-8") as f:
                await f.write(data)
            os.replace(temp_file, self._config_file)
        except Exception as e:
            log.error(f"Error saving config file {self._config_file}: {e}")
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except Exception:
                    pass

    async def _sync_credentials_from_disk(self, mode: str) -> None:
        mode_dir = self._geminicli_dir if mode == "geminicli" else self._antigravity_dir

        root_json_files = [
            f for f in os.listdir(self._credentials_dir)
            if f.endswith(".json") and os.path.isfile(os.path.join(self._credentials_dir, f)) and f not in ("geminicli_state.json", "antigravity_state.json", "config.json", "token_stats.json")
        ]
        if root_json_files:
            os.makedirs(mode_dir, exist_ok=True)
            for f in root_json_files:
                src = os.path.join(self._credentials_dir, f)
                dest = os.path.join(mode_dir, f)
                if not os.path.exists(dest):
                    try:
                        os.rename(src, dest)
                        log.info(f"Moved credential {f} into {mode_dir}")
                    except Exception as e:
                        log.warning(f"Could not move {f}: {e}")

        disk_files = set(f for f in os.listdir(mode_dir) if f.endswith(".json")) if os.path.exists(mode_dir) else set()
        state_dict = self._states[mode]
        updated = False

        for idx, fname in enumerate(sorted(disk_files)):
            if fname not in state_dict:
                default_state = {
                    "disabled": False,
                    "error_codes": [],
                    "error_messages": [],
                    "last_success": time.time(),
                    "user_email": None,
                    "model_cooldowns": {},
                    "tier": "pro",
                    "rotation_order": idx,
                }
                if mode == "geminicli":
                    default_state["preview"] = True
                else:
                    default_state["enable_credit"] = False

                state_dict[fname] = default_state
                updated = True

        to_delete = [fname for fname in state_dict if fname not in disk_files]
        for fname in to_delete:
            del state_dict[fname]
            updated = True

        if updated:
            await self._save_states(mode)

    async def _migrate_filenames_to_emails(self):
        for mode in ("geminicli", "antigravity"):
            state_dict = self._states[mode]
            mode_dir = self._geminicli_dir if mode == "geminicli" else self._antigravity_dir
            if not os.path.exists(mode_dir):
                continue

            updated = False
            for old_fname in list(state_dict.keys()):
                st = state_dict[old_fname]
                email = st.get("user_email")

                if not email:
                    filepath = os.path.join(mode_dir, old_fname)
                    if os.path.exists(filepath):
                        try:
                            with open(filepath, "r", encoding="utf-8") as f:
                                cdata = json.load(f)
                            local_email = cdata.get("user_email") or cdata.get("email") or cdata.get("account")
                            if local_email and isinstance(local_email, str) and "@" in local_email:
                                email = local_email
                                st["user_email"] = email
                            elif "@" in old_fname and old_fname.endswith(".json"):
                                email = old_fname[:-5]
                                st["user_email"] = email
                        except Exception as e:
                            log.warning(f"读取凭证文件 {old_fname} 提取邮箱失败: {e}")

                if email:
                    new_fname = f"{email}.json"
                    if old_fname != new_fname:
                        old_path = os.path.join(mode_dir, old_fname)
                        new_path = os.path.join(mode_dir, new_fname)
                        try:
                            if os.path.exists(old_path):
                                if os.path.exists(new_path):
                                    os.remove(old_path)
                                    log.info(f"重复的凭证文件已存在，删除旧文件: {old_fname}")
                                else:
                                    os.rename(old_path, new_path)
                                    log.info(f"重命名凭证文件: {old_fname} -> {new_fname}")

                                if new_fname in state_dict and new_fname != old_fname:
                                    old_st = state_dict.pop(old_fname)
                                    for k, v in old_st.items():
                                        if k not in state_dict[new_fname] or state_dict[new_fname][k] is None:
                                            state_dict[new_fname][k] = v
                                    # 如果旧凭证已被禁用，迁移后保持禁用状态
                                    if old_st.get("disabled", False):
                                        state_dict[new_fname]["disabled"] = True
                                else:
                                    state_dict[new_fname] = state_dict.pop(old_fname)
                                updated = True
                        except Exception as e:
                            log.error(f"重命名凭证文件失败 {old_fname} -> {new_fname}: {e}")

            if updated:
                await self._save_states(mode)

    def _get_credential_path(self, filename: str, mode: str) -> str:
        filename = os.path.basename(filename)
        mode_dir = self._geminicli_dir if mode == "geminicli" else self._antigravity_dir
        return os.path.join(mode_dir, filename)

    async def close(self) -> None:
        self._initialized = False
        log.debug("JSON storage closed")

    def _ensure_initialized(self):
        if not self._initialized:
            raise RuntimeError("Storage adapter not initialized")

    # ============ 凭证管理 ============

    async def store_credential(self, filename: str, credential_data: Dict[str, Any], mode: str = "geminicli") -> bool:
        self._ensure_initialized()
        filename = os.path.basename(filename)
        mode_dir = self._geminicli_dir if mode == "geminicli" else self._antigravity_dir
        os.makedirs(mode_dir, exist_ok=True)
        filepath = self._get_credential_path(filename, mode)

        try:
            async with self._lock:
                temp_path = filepath + ".tmp"
                data_str = json.dumps(credential_data, ensure_ascii=False, indent=2)
                async with aiofiles.open(temp_path, "w", encoding="utf-8") as f:
                    await f.write(data_str)
                os.replace(temp_path, filepath)

                state_dict = self._states[mode]
                if filename not in state_dict:
                    next_order = len(state_dict)
                    default_state = {
                        "disabled": False,
                        "error_codes": [],
                        "error_messages": [],
                        "last_success": time.time(),
                        "user_email": None,
                        "model_cooldowns": {},
                        "tier": "pro",
                        "rotation_order": next_order,
                    }
                    if mode == "geminicli":
                        default_state["preview"] = True
                    else:
                        default_state["enable_credit"] = False
                    state_dict[filename] = default_state
                    await self._save_states(mode)

            log.debug(f"Stored credential JSON: {filename} (mode={mode})")
            return True
        except Exception as e:
            log.error(f"Error storing credential JSON {filename}: {e}")
            return False

    async def get_credential(self, filename: str, mode: str = "geminicli") -> Optional[Dict[str, Any]]:
        self._ensure_initialized()
        filename = os.path.basename(filename)
        filepath = self._get_credential_path(filename, mode)

        if not os.path.exists(filepath):
            return None

        try:
            async with aiofiles.open(filepath, "r", encoding="utf-8") as f:
                content = await f.read()
                return json.loads(content)
        except Exception as e:
            log.error(f"Error reading credential JSON {filename}: {e}")
            return None

    async def list_credentials(self, mode: str = "geminicli") -> List[str]:
        self._ensure_initialized()
        state_dict = self._states[mode]
        sorted_files = sorted(state_dict.keys(), key=lambda k: state_dict[k].get("rotation_order", 0))
        return sorted_files

    async def delete_credential(self, filename: str, mode: str = "geminicli") -> bool:
        self._ensure_initialized()
        filename = os.path.basename(filename)
        filepath = self._get_credential_path(filename, mode)

        async with self._lock:
            removed = False
            if os.path.exists(filepath):
                try:
                    os.remove(filepath)
                    removed = True
                except Exception as e:
                    log.error(f"Error deleting file {filepath}: {e}")

            if filename in self._states[mode]:
                del self._states[mode][filename]
                await self._save_states(mode)
                removed = True

        return removed

    # ============ 状态管理 ============

    async def update_credential_state(self, filename: str, state_updates: Dict[str, Any], mode: str = "geminicli") -> bool:
        self._ensure_initialized()
        filename = os.path.basename(filename)

        async with self._lock:
            state_dict = self._states[mode]
            if filename not in state_dict:
                log.warning(f"Credential {filename} state not found for update")
                return False

            st = state_dict[filename]
            for k, v in state_updates.items():
                if k in self.STATE_FIELDS:
                    if k == "enable_credit" and mode != "antigravity":
                        continue
                    st[k] = v

            email = st.get("user_email")
            if email:
                new_filename = f"{email}.json"
                if filename != new_filename:
                    mode_dir = self._geminicli_dir if mode == "geminicli" else self._antigravity_dir
                    old_path = os.path.join(mode_dir, filename)
                    new_path = os.path.join(mode_dir, new_filename)
                    try:
                        if os.path.exists(old_path):
                            if os.path.exists(new_path):
                                os.remove(old_path)
                                log.info(f"Duplicate credential file {new_filename} already exists, removed old file {filename}")
                            else:
                                os.rename(old_path, new_path)
                                log.info(f"Renamed credential file from {filename} to {new_filename} on state update")

                            if new_filename in state_dict and new_filename != filename:
                                old_st = state_dict.pop(filename)
                                for k, v in old_st.items():
                                    if k not in state_dict[new_filename] or state_dict[new_filename][k] is None:
                                        state_dict[new_filename][k] = v
                            else:
                                state_dict[new_filename] = state_dict.pop(filename)
                    except Exception as e:
                        log.error(f"Failed to rename credential file {filename} to {new_filename} on state update: {e}")

            await self._save_states(mode)
            return True

    async def get_credential_state(self, filename: str, mode: str = "geminicli") -> Dict[str, Any]:
        self._ensure_initialized()
        filename = os.path.basename(filename)
        st = self._states[mode].get(filename)

        if not st:
            default_state = {
                "disabled": False,
                "error_codes": [],
                "last_success": time.time(),
                "user_email": None,
                "model_cooldowns": {},
                "tier": "pro",
            }
            if mode == "geminicli":
                default_state["preview"] = True
            else:
                default_state["enable_credit"] = False
            return default_state

        res = {
            "disabled": bool(st.get("disabled", False)),
            "error_codes": st.get("error_codes", []),
            "last_success": st.get("last_success", time.time()),
            "user_email": st.get("user_email"),
            "model_cooldowns": st.get("model_cooldowns", {}),
            "tier": st.get("tier", "pro"),
        }
        if mode == "geminicli":
            res["preview"] = bool(st.get("preview", True))
        else:
            res["enable_credit"] = bool(st.get("enable_credit", False))
            res["quota_groups"] = st.get("quota_groups", [])

        return res

    async def get_all_credential_states(self, mode: str = "geminicli") -> Dict[str, Dict[str, Any]]:
        self._ensure_initialized()
        current_time = time.time()
        result = {}

        for filename, st in self._states[mode].items():
            model_cooldowns = st.get("model_cooldowns", {})
            if model_cooldowns:
                model_cooldowns = {k: v for k, v in model_cooldowns.items() if v > current_time}

            item = {
                "disabled": bool(st.get("disabled", False)),
                "error_codes": st.get("error_codes", []),
                "last_success": st.get("last_success", current_time),
                "user_email": st.get("user_email"),
                "model_cooldowns": model_cooldowns,
                "tier": st.get("tier", "pro"),
            }
            if mode == "geminicli":
                item["preview"] = bool(st.get("preview", True))
            else:
                item["enable_credit"] = bool(st.get("enable_credit", False))
                item["quota_groups"] = st.get("quota_groups", [])

            result[filename] = item

        return result

    def _extract_weekly_quota_info(self, st: Dict[str, Any]) -> Tuple[float, float]:
        """
        从凭证状态中提取 Weekly 限额的剩余比例与重置 Unix 时间戳。
        Returns:
            (remaining_fraction, reset_timestamp)
            - remaining_fraction: 0.0 ~ 1.0 (无信息时默认 1.0)
            - reset_timestamp: float (无信息或解析失败时默认 float('inf'))
        """
        quota_groups = st.get("quota_groups", [])
        if not isinstance(quota_groups, list) or not quota_groups:
            return 1.0, float("inf")

        best_rem = None
        best_reset_ts = float("inf")

        for group in quota_groups:
            if not isinstance(group, dict):
                continue
            buckets = group.get("buckets", [])
            if not isinstance(buckets, list):
                continue

            for bucket in buckets:
                if not isinstance(bucket, dict):
                    continue

                display_name = str(bucket.get("displayName", "")).lower()
                description = str(bucket.get("description", "")).lower()
                window = str(bucket.get("window", "")).lower()
                bucket_id = str(bucket.get("bucketId", "")).lower()

                # 匹配周限额标识 (weekly / 7d / 周)
                is_weekly = (
                    "week" in display_name
                    or "week" in description
                    or "week" in window
                    or "week" in bucket_id
                    or "7d" in window
                    or "7_day" in window
                    or "周" in display_name
                    or "周" in description
                )

                if is_weekly:
                    try:
                        rem = float(bucket.get("remainingFraction", 1.0))
                    except (ValueError, TypeError):
                        rem = 1.0
                    rem = max(0.0, min(1.0, rem))

                    reset_time_raw = bucket.get("resetTimeRaw") or bucket.get("resetTime")
                    reset_ts = float("inf")

                    if reset_time_raw:
                        try:
                            raw_str = str(reset_time_raw).strip()
                            if raw_str.endswith("Z"):
                                raw_str = raw_str[:-1] + "+00:00"
                            dt = datetime.fromisoformat(raw_str)
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=timezone.utc)
                            reset_ts = dt.timestamp()
                        except Exception:
                            reset_ts = float("inf")

                    best_rem = rem
                    best_reset_ts = reset_ts
                    break

            if best_rem is not None:
                break

        if best_rem is None:
            return 1.0, float("inf")

        return best_rem, best_reset_ts

    def _credential_schedule_key(self, state_dict: Dict[str, Dict[str, Any]], fname: str):
        st = state_dict.get(fname, {})
        rem_fraction, reset_ts = self._extract_weekly_quota_info(st)

        # 1. 是否有剩余周额度：rem_fraction > 0 为 0 (优先使用有额度的)，0% 额度为 1 (沉底)
        has_quota = 0 if rem_fraction > 0 else 1

        # 2. 周限额剩余比例阶梯分组 (以 5% 为一阶梯):
        # 数值越小排在越前面 -> 优先使用周限额最多的账号
        quota_tier = -round(rem_fraction * 20) / 20

        # 3. 周重置时间戳 reset_ts：
        # 同一阶梯内，重置时间戳越小（越早重置）排在越前面 -> 重置日期临近优先
        
        # 4. 原始 rotation_order 作为平局兜底
        rot_order = st.get("rotation_order", 0)

        return (has_quota, quota_tier, reset_ts, rot_order)

    async def get_next_available_credential(
        self, mode: str = "geminicli", model_name: Optional[str] = None
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        self._ensure_initialized()
        state_dict = self._states[mode]
        current_time = time.time()

        sorted_files = sorted(state_dict.keys(), key=lambda f: self._credential_schedule_key(state_dict, f))

        for fname in sorted_files:
            st = state_dict[fname]
            if st.get("disabled", False):
                continue

            # 排除未验证(unverified)账号
            if st.get("tier") == "unverified":
                continue

            if model_name:
                model_cooldowns = st.get("model_cooldowns", {})
                cooldown_until = model_cooldowns.get(model_name, 0)
                if cooldown_until > current_time:
                    continue

            cred_data = await self.get_credential(fname, mode=mode)
            if cred_data:
                return fname, cred_data

        return None

    async def get_available_credentials_list(self) -> List[str]:
        self._ensure_initialized()
        state_dict = self._states["geminicli"]
        available = [f for f, st in state_dict.items() if not st.get("disabled", False)]
        available.sort(key=lambda f: self._credential_schedule_key(state_dict, f))
        return available

    # ============ 摘要与查询 ============

    async def get_credentials_summary(
        self,
        offset: int = 0,
        limit: Optional[int] = None,
        status_filter: str = "all",
        mode: str = "geminicli",
        error_code_filter: Optional[str] = None,
        cooldown_filter: Optional[str] = None,
        preview_filter: Optional[str] = None,
        tier_filter: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._ensure_initialized()
        state_dict = self._states[mode]
        current_time = time.time()

        global_stats = {"total": len(state_dict), "normal": 0, "disabled": 0}
        for st in state_dict.values():
            if st.get("disabled", False):
                global_stats["disabled"] += 1
            else:
                global_stats["normal"] += 1

        filter_value = None
        filter_int = None
        filter_none = False
        if error_code_filter and str(error_code_filter).strip().lower() != "all":
            if str(error_code_filter).strip().lower() == "none":
                filter_none = True
            else:
                filter_value = str(error_code_filter).strip()
                try:
                    filter_int = int(filter_value)
                except ValueError:
                    filter_int = None

        current_selected, current_selected_time = None, None

        all_summaries = []
        sorted_files = sorted(state_dict.keys(), key=lambda f: state_dict[f].get("rotation_order", 0))

        for fname in sorted_files:
            st = state_dict[fname]
            is_disabled = bool(st.get("disabled", False))

            if status_filter == "enabled" and is_disabled:
                continue
            if status_filter == "disabled" and not is_disabled:
                continue

            error_codes = st.get("error_codes", [])
            if filter_none and error_codes:
                continue
            if filter_value:
                match = False
                for code in error_codes:
                    if code == filter_value or code == filter_int:
                        match = True
                        break
                    if isinstance(code, str) and filter_int is not None:
                        try:
                            if int(code) == filter_int:
                                match = True
                                break
                        except ValueError:
                            pass
                if not match:
                    continue

            model_cooldowns = st.get("model_cooldowns", {})
            active_cooldowns = {k: v for k, v in model_cooldowns.items() if v > current_time} if model_cooldowns else {}

            tier = st.get("tier", "pro")
            if tier_filter and tier_filter in ("free", "pro", "ultra"):
                if tier != tier_filter:
                    continue

            fname_base = os.path.basename(fname)
            is_selected = bool(current_selected and (fname_base == current_selected or fname == current_selected))

            summary = {
                "filename": fname,
                "disabled": is_disabled,
                "error_codes": error_codes,
                "last_success": st.get("last_success", current_time),
                "user_email": st.get("user_email"),
                "user_name": st.get("user_name"),
                "rotation_order": st.get("rotation_order", 0),
                "model_cooldowns": active_cooldowns,
                "tier": tier,
                "is_selected": is_selected,
            }

            if mode == "geminicli":
                preview_val = bool(st.get("preview", True))
                summary["preview"] = preview_val
                if preview_filter == "preview" and not preview_val:
                    continue
                elif preview_filter == "no_preview" and preview_val:
                    continue
            else:
                summary["enable_credit"] = bool(st.get("enable_credit", False))
                summary["quota_groups"] = st.get("quota_groups", [])

            if cooldown_filter == "in_cooldown" and not active_cooldowns:
                continue
            if cooldown_filter == "no_cooldown" and active_cooldowns:
                continue

            all_summaries.append(summary)

        total_count = len(all_summaries)
        if limit is not None:
            summaries = all_summaries[offset : offset + limit]
        else:
            summaries = all_summaries[offset:]

        return {
            "items": summaries,
            "total": total_count,
            "offset": offset,
            "limit": limit,
            "stats": global_stats,
            "current_selected": current_selected,
            "current_selected_time": current_selected_time,
        }

    async def get_credential_errors(self, filename: str, mode: str = "geminicli") -> Dict[str, Any]:
        self._ensure_initialized()
        filename = os.path.basename(filename)
        st = self._states[mode].get(filename)

        if st:
            return {
                "filename": filename,
                "error_codes": st.get("error_codes", []),
                "error_messages": st.get("error_messages", []),
            }

        return {"filename": filename, "error_codes": [], "error_messages": []}

    # ============ 冷却与成功记录 ============

    async def set_model_cooldown(
        self, filename: str, model_name: str, cooldown_until: Optional[float], mode: str = "geminicli"
    ) -> bool:
        self._ensure_initialized()
        filename = os.path.basename(filename)

        async with self._lock:
            st = self._states[mode].get(filename)
            if not st:
                return False

            cooldowns = st.get("model_cooldowns", {})
            if cooldown_until is None:
                cooldowns.pop(model_name, None)
            else:
                cooldowns[model_name] = cooldown_until
            st["model_cooldowns"] = cooldowns

            await self._save_states(mode)
            return True

    async def clear_all_model_cooldowns(self, filename: str, mode: str = "geminicli") -> bool:
        self._ensure_initialized()
        filename = os.path.basename(filename)

        async with self._lock:
            st = self._states[mode].get(filename)
            if not st:
                return False

            st["model_cooldowns"] = {}
            await self._save_states(mode)
            return True

    async def record_success(self, filename: str, model_name: Optional[str] = None, mode: str = "geminicli") -> None:
        self._ensure_initialized()
        filename = os.path.basename(filename)

        async with self._lock:
            st = self._states[mode].get(filename)
            if not st:
                return

            updated = False
            if st.get("error_codes"):
                st["last_success"] = time.time()
                st["error_codes"] = []
                st["error_messages"] = []
                updated = True

            if model_name:
                cooldowns = st.get("model_cooldowns", {})
                if model_name in cooldowns:
                    cooldowns.pop(model_name)
                    st["model_cooldowns"] = cooldowns
                    updated = True

            if updated:
                await self._save_states(mode)

    # ============ 配置管理 ============

    async def set_config(self, key: str, value: Any) -> bool:
        self._ensure_initialized()
        async with self._lock:
            self._config_cache[key] = value
            await self._save_config()
        return True

    async def get_config(self, key: str, default: Any = None) -> Any:
        self._ensure_initialized()
        return self._config_cache.get(key, default)

    async def get_all_config(self) -> Dict[str, Any]:
        self._ensure_initialized()
        return self._config_cache.copy()

    async def delete_config(self, key: str) -> bool:
        self._ensure_initialized()
        async with self._lock:
            if key in self._config_cache:
                del self._config_cache[key]
                await self._save_config()
                return True
        return False

    def get_backend_type(self) -> str:
        """获取当前存储后端类型"""
        return "json"

    def get_database_info(self) -> Dict[str, Any]:
        return {
            "backend_type": "json",
            "credentials_dir": self._credentials_dir,
            "initialized": self._initialized,
        }

    async def get_backend_info(self) -> Dict[str, Any]:
        self._ensure_initialized()
        return self.get_database_info()

    async def export_credential_to_json(self, filename: str, output_path: str = None) -> bool:
        """将凭证导出为 JSON 文件"""
        self._ensure_initialized()
        credential_data = await self.get_credential(filename)
        if credential_data is None:
            return False

        if output_path is None:
            output_path = f"{filename}.json"

        try:
            async with aiofiles.open(output_path, "w", encoding="utf-8") as f:
                await f.write(json.dumps(credential_data, indent=2, ensure_ascii=False))
            return True
        except Exception:
            return False

    async def import_credential_from_json(self, json_path: str, filename: str = None) -> bool:
        """从 JSON 文件导入凭证"""
        self._ensure_initialized()
        try:
            async with aiofiles.open(json_path, "r", encoding="utf-8") as f:
                content = await f.read()

            credential_data = json.loads(content)
            if filename is None:
                filename = os.path.basename(json_path)

            return await self.store_credential(filename, credential_data)
        except Exception:
            return False


# 全局存储单例
_storage_instance: Optional[Storage] = None


async def get_storage() -> Storage:
    """获取全局存储单例实例"""
    global _storage_instance

    if _storage_instance is None:
        _storage_instance = Storage()
        await _storage_instance.initialize()

    return _storage_instance


async def close_storage():
    """关闭全局存储单例"""
    global _storage_instance

    if _storage_instance:
        await _storage_instance.close()
        _storage_instance = None