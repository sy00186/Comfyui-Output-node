// Vates：将 .dct 拖入 ComfyUI 画布时，调用后端读取嵌入的 prompt/extra_pnginfo JSON，并尽量还原节点图。
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

/** @param {unknown} blob */
function pickWorkflowGraph(blob) {
	if (!blob || typeof blob !== "object") return null;
	/** @type {any} */
	const o = blob;
	if (o.workflow && o.workflow.nodes) return o.workflow;
	const ex = o.extra_pnginfo;
	if (ex && typeof ex === "object" && !Array.isArray(ex)) {
		if (ex.workflow && ex.workflow.nodes) return ex.workflow;
	}
	return null;
}

/** @param {string} text */
async function loadFromEmbeddedJson(text) {
	let data;
	try {
		data = JSON.parse(text);
	} catch {
		throw new Error("嵌入内容不是合法 JSON");
	}
	const graph = pickWorkflowGraph(data);
	if (graph && typeof app.loadGraphData === "function") {
		await app.loadGraphData(graph);
		return;
	}
	/** 部分版本支持仅 API prompt 导入 */
	if (data && typeof data === "object" && data.prompt != null) {
		const anyApp = /** @type {any} */ (app);
		if (typeof anyApp.loadApiFormat === "function")
			await anyApp.loadApiFormat(data.prompt);
		else if (typeof anyApp.importApiJson === "function")
			await anyApp.importApiJson(data.prompt);
		else
			throw new Error(
				"嵌入数据不含带 nodes 的 workflow；当前前端不支持自动导入纯 API prompt",
			);
		return;
	}
	throw new Error("嵌入 JSON 中未找到 workflow.nodes");
}

app.registerExtension({
	name: "Vates.DctWorkflowDrop",
	setup() {
		window.addEventListener(
			"drop",
			async (e) => {
				const f = e.dataTransfer?.files?.[0];
				if (!f || !String(f.name).toLowerCase().endsWith(".dct")) return;

				e.preventDefault();
				e.stopPropagation();

				const fd = new FormData();
				fd.append("file", f, f.name);
				let res;
				try {
					res = await api.fetchApi("/vates/extract_workflow", {
						method: "POST",
						body: fd,
					});
				} catch (err) {
					console.error("[Vates] extract_workflow 请求失败", err);
					alert(`[Vates] 无法请求服务器解析 .dct：${err}`);
					return;
				}

				let j;
				try {
					j = await res.json();
				} catch {
					alert("[Vates] 服务器返回非 JSON");
					return;
				}
				if (!res.ok || !j.ok) {
					alert(`[Vates] ${j.error || res.statusText || "解析失败"}`);
					return;
				}
				if (j.embedded == null || j.embedded === "") {
					alert("[Vates] 此 .dct 未嵌入工作流元数据（保存时需图内包含 Vates Save 且提供 prompt）");
					return;
				}
				try {
					await loadFromEmbeddedJson(j.embedded);
				} catch (err) {
					console.error("[Vates] 加载工作流失败", err);
					alert(`[Vates] 无法还原画布：${err}`);
				}
			},
			true,
		);
	},
});
