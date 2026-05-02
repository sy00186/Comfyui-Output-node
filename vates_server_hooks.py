"""
ComfyUI PromptServer 扩展：供前端拖放 `.dct` 时解析嵌入的工作流 JSON。

仅在 `from server import PromptServer` 成功时注册；独立运行 `check_vates` 或纯 Python 环境会静默跳过。
"""

from __future__ import annotations

import logging
import os
import tempfile

logger = logging.getLogger(__name__)

_ROUTES_REGISTERED = False


def register_vates_server_routes() -> None:
    """向 PromptServer 注册 `POST /vates/extract_workflow`（multipart 字段 `file`）。"""
    global _ROUTES_REGISTERED
    if _ROUTES_REGISTERED:
        return
    try:
        from aiohttp import web
        from server import PromptServer
    except ImportError:
        logger.debug("Vates: 非 ComfyUI 环境，跳过 /vates/extract_workflow 注册")
        return

    inst = getattr(PromptServer, "instance", None)
    if inst is None:
        logger.warning("Vates: PromptServer.instance 不可用，跳过路由注册")
        return

    routes = inst.routes

    @routes.post("/vates/extract_workflow")
    async def vates_extract_workflow(request: web.Request):
        import vates_nodes  # 惰性导入，避免非 Comfy 环境下的循环问题

        vates_nodes._ensure_vates_loaded()
        vc = vates_nodes.vates_core
        reader = await request.multipart()
        payload: bytes | None = None
        async for field in reader:
            if field.name == "file":
                payload = await field.read()
                break
        if not payload:
            return web.json_response(
                {"ok": False, "error": "缺少 multipart 字段 file"},
                status=400,
            )
        tmp_path: str | None = None
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".dct")
            try:
                os.write(fd, payload)
            finally:
                os.close(fd)
            embedded = vc.read_embedded_workflow_json(tmp_path)
        except Exception as exc:
            logger.exception("Vates extract_workflow")
            return web.json_response({"ok": False, "error": str(exc)}, status=500)
        finally:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        return web.json_response({"ok": True, "embedded": embedded})

    _ROUTES_REGISTERED = True
    logger.info("Vates: 已注册 POST /vates/extract_workflow")
