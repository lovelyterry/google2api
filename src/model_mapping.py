import json
import os
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone
from src.log import log

LEGACY_MAPPING_FILE_PATH = "model_mappings.json"


class ModelMappingManager:
    """内存中维护的模型映射管理器（单例，融合在 creds/config.json 中持久化）"""

    _instance: Optional['ModelMappingManager'] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ModelMappingManager, cls).__new__(cls)
            cls._instance._dynamic_map: Dict[str, Dict[str, Any]] = {}
            cls._instance._custom_map: Dict[str, str] = {}
            cls._instance._mapping_stats: Dict[str, int] = {}
            # router_type -> fallback_model
            cls._instance._fallback_map: Dict[str, str] = {}
            cls._instance._load_from_disk()
        return cls._instance

    def _get_config_path(self) -> str:
        credentials_dir = os.getenv("CREDENTIALS_DIR", "./creds")
        return os.path.join(credentials_dir, "config.json")

    def _load_from_disk(self):
        """从 creds/config.json 恢复模型映射配置（支持自动迁移旧 model_mappings.json）"""
        try:
            # 1. 尝试从旧独立文件 model_mappings.json 迁移
            if os.path.exists(LEGACY_MAPPING_FILE_PATH):
                try:
                    with open(LEGACY_MAPPING_FILE_PATH, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        self._custom_map = data.get("custom_map", {})
                        self._fallback_map = data.get("fallback_map", {})
                    log.info(
                        f"[MODEL MAP] 从旧文件 {LEGACY_MAPPING_FILE_PATH} 迁移映射数据到 creds/config.json")
                    self._save_to_disk()
                    try:
                        os.remove(LEGACY_MAPPING_FILE_PATH)
                    except Exception:
                        pass
                    return
                except Exception as e:
                    log.warning(f"[MODEL MAP] 迁移旧 model_mappings.json 失败: {e}")

            # 2. 从 creds/config.json 读取
            config_path = self._get_config_path()
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._custom_map = data.get("custom_map", {})
                    self._fallback_map = data.get("fallback_map", {})
                log.info(
                    f"[MODEL MAP] 从 {config_path} 成功加载 {len(self._custom_map)} 条自定义映射规则")
        except Exception as e:
            log.error(f"[MODEL MAP] 加载本地映射配置失败: {e}")

    def _save_to_disk(self):
        """持久化保存自定义映射与兜底配置到 creds/config.json"""
        try:
            config_path = self._get_config_path()
            config_data = {}
            if os.path.exists(config_path):
                try:
                    with open(config_path, "r", encoding="utf-8") as f:
                        config_data = json.load(f)
                except Exception:
                    config_data = {}

            config_data["custom_map"] = self._custom_map
            config_data["fallback_map"] = self._fallback_map

            os.makedirs(os.path.dirname(config_path), exist_ok=True)
            temp_path = config_path + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(config_data, f, ensure_ascii=False, indent=2)
            os.replace(temp_path, config_path)

            # 同步更新底层 Storage 的内存配置缓存，避免保存常规设置时冲掉模型映射
            try:
                from src.storage import _storage_instance
                if _storage_instance and hasattr(_storage_instance, "_config_cache") and isinstance(_storage_instance._config_cache, dict):
                    _storage_instance._config_cache["custom_map"] = self._custom_map
                    _storage_instance._config_cache["fallback_map"] = self._fallback_map
            except Exception:
                pass
        except Exception as e:
            log.error(f"[MODEL MAP] 保存映射数据到 {config_path} 失败: {e}")

    def record_mapping(self, requested_model: str, target_model: str, router_type: str = "default"):
        """
        记录一次模型映射

        Args:
            requested_model: 前端请求的模型名称 (如 "假流式/gemini-3.6-flash-high")
            target_model: 实际发往后端的模型名称 (如 "gemini-3.6-flash-high")
            router_type: 路由类型 (如 "antigravity", "geminicli", "vertex")
        """
        if not requested_model or not target_model:
            return

        key = f"{router_type}:{requested_model}"
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        # 记录实时映射关系
        self._dynamic_map[key] = {
            "requested_model": requested_model,
            "target_model": target_model,
            "router_type": router_type,
            "last_used_at": now
        }

        # 统计调用次数
        self._mapping_stats[key] = self._mapping_stats.get(key, 0) + 1

    def resolve_model(self, requested_model: str, router_type: str = "default") -> str:
        """
        根据自定义映射或兜底逻辑解析目标模型
        """
        if not requested_model:
            if router_type in self._fallback_map and self._fallback_map[router_type]:
                return self._fallback_map[router_type]
            if "default" in self._fallback_map and self._fallback_map["default"]:
                return self._fallback_map["default"]
            return requested_model

        key = f"{router_type}:{requested_model}"
        # 1. 优先使用专属精确自定义映射
        if key in self._custom_map:
            return self._custom_map[key]
        # 2. 兼容不带 router_type 的通用精确自定义映射
        if requested_model in self._custom_map:
            return self._custom_map[requested_model]

        # 3. 通配符/模糊规则匹配 (如 claude-3-7*, gpt-4*, *sonnet* 等)
        import fnmatch
        for pattern_key, target in self._custom_map.items():
            pattern = pattern_key
            if ":" in pattern_key:
                p_router, p_model = pattern_key.split(":", 1)
                if p_router != router_type and p_router != "default":
                    continue
                pattern = p_model

            if ("*" in pattern or "?" in pattern) and fnmatch.fnmatch(requested_model.lower(), pattern.lower()):
                log.info(f"[MODEL MAP] 请求模型 [{requested_model}] 命中通配符规则 [{pattern}] -> {target}")
                return target

        # 4. 若配置了全局/路由兜底目标模型，对未单独配置映射的第三方模型（如 Claude Code、GPT 等）自动应用兜底
        fallback = self._fallback_map.get(router_type) or self._fallback_map.get("default")
        if fallback:
            req_lower = requested_model.lower()
            if req_lower.startswith(("claude", "gpt", "o1", "o3", "deepseek", "qwen", "anthropic")) or "claude" in req_lower:
                log.info(f"[MODEL MAP] 第三方请求模型 [{requested_model}] 未匹配独立规则，自动兜底至 -> {fallback}")
                return fallback

        return requested_model

    def set_fallback_model(self, fallback_model: str, router_type: str = "antigravity"):
        """设置某个路由或全局的兜底模型"""
        self._fallback_map[router_type] = fallback_model.strip()
        self._save_to_disk()
        log.info(f"[MODEL MAP] 设置 [{router_type}] 兜底模型为: {fallback_model}")

    def get_fallback_models(self) -> Dict[str, str]:
        """获取兜底模型配置"""
        return self._fallback_map

    def set_custom_mapping(self, requested_model: str, target_model: str, router_type: str = "default"):
        """手动配置/覆盖特定映射关系"""
        key = f"{router_type}:{requested_model}"
        self._custom_map[key] = target_model
        self._save_to_disk()
        log.info(f"[MODEL MAP] 设置自定义映射规则: {key} -> {target_model}")

    def remove_custom_mapping(self, requested_model: str, router_type: str = "default"):
        """移除手动映射规则"""
        key = f"{router_type}:{requested_model}"
        popped = self._custom_map.pop(key, None)
        popped_unprefixed = self._custom_map.pop(requested_model, None)
        if popped is not None or popped_unprefixed is not None:
            self._save_to_disk()

    def clear_custom_mappings(self):
        """清空所有自定义映射规则"""
        self._custom_map.clear()
        self._save_to_disk()
        log.info("[MODEL MAP] 已清空所有自定义模型映射记录")

    def clear_dynamic_mappings(self):
        """清空动态抓取的映射记录与统计"""
        self._dynamic_map.clear()
        self._mapping_stats.clear()
        log.info("[MODEL MAP] 已清空所有动态模型映射记录")

    def get_all_mappings(self) -> Dict[str, Any]:
        """获取当前内存中记录的所有模型映射状态"""
        dynamic_list = []
        for key, info in self._dynamic_map.items():
            dynamic_list.append({
                "key": key,
                "requested_model": info["requested_model"],
                "target_model": info["target_model"],
                "router_type": info["router_type"],
                "count": self._mapping_stats.get(key, 0),
                "last_used_at": info["last_used_at"]
            })

        custom_list = []
        for key, target in self._custom_map.items():
            parts = key.split(":", 1)
            rtype = parts[0] if len(parts) == 2 else "default"
            req = parts[1] if len(parts) == 2 else key
            custom_list.append({
                "key": key,
                "requested_model": req,
                "target_model": target,
                "router_type": rtype
            })

        return {
            "dynamic_mappings": dynamic_list,
            "custom_mappings": custom_list,
            "fallback_mappings": self._fallback_map
        }


# 全局单例
model_mapping_manager = ModelMappingManager()
