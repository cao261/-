package microbench.tablet;

import android.webkit.WebResourceResponse;

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * 把网页里的 /dav/* 请求翻译成坚果云 WebDAV 请求。
 *
 * 约定（PathHandler 拿不到 query string，所以用路径前缀区分）：
 *   GET /dav/list/<dir>      ->  列 vault 目录，返回 JSON 数组
 *   GET /dav/file/<path>     ->  读 vault 里的文件，原样返回字节
 *   GET /dav/sync/list/<dir> ->  列**同步根**（译文快照 / 批注所在的目录）
 *   GET /dav/sync/file/<path>->  读同步根里的文件
 *
 * ⚠ 同步根不是 vault 的一部分 —— 它是 vault 根的**兄弟目录**
 *   （「我的坚果云/MicroBench同步」 vs 「我的坚果云/MicroBench工作台」）。
 *   桌面端把译文快照和批注放在那里，两台设备共享同一份。
 *   平板想拿到译文（这是它跟「直接用微信打开 PDF」的根本区别），
 *   就必须能读到这个目录。
 *
 * 写操作走 JS 桥（{@link DavBridge}），因为这里只能处理 GET。
 *
 * 刻意不实现 androidx 的 PathHandler 接口 —— 那样就得拖 androidx 依赖，
 * 而手工打包 APK 时没有 Gradle 去拉 Maven 依赖。
 */
public class DavPathHandler {

    private final Prefs prefs;

    public DavPathHandler(Prefs prefs) {
        this.prefs = prefs;
    }

    public WebResourceResponse handle(String path) {
        try {
            if (path.startsWith("/dav/sync/list/")) {
                return listResponse(decode(path.substring("/dav/sync/list/".length())), true);
            }
            if (path.startsWith("/dav/sync/file/")) {
                return fileResponse(decode(path.substring("/dav/sync/file/".length())), true);
            }
            if (path.startsWith("/dav/list/")) {
                return listResponse(decode(path.substring("/dav/list/".length())), false);
            }
            if (path.startsWith("/dav/file/")) {
                return fileResponse(decode(path.substring("/dav/file/".length())), false);
            }
            return new WebResourceResponse("text/plain", "UTF-8", 404, "Not Found",
                    new HashMap<String, String>(),
                    new ByteArrayInputStream("no such dav route".getBytes(StandardCharsets.UTF_8)));
        } catch (Exception e) {
            return new WebResourceResponse("text/plain", "UTF-8", 500, "DAV Error",
                    new HashMap<String, String>(),
                    new ByteArrayInputStream(
                            ("DAV error: " + e.getMessage()).getBytes(StandardCharsets.UTF_8)));
        }
    }

    /**
     * path 来自 Uri.getPath()，**已经解码过了**，所以这里不能再解一次 ——
     * 二次解码会把文件名里合法的 % 字符吃坏。
     */
    private static String decode(String s) {
        return s == null ? "" : s;
    }

    private WebResourceResponse listResponse(String dir, boolean syncRoot) throws Exception {
        DavClient c = syncRoot ? prefs.syncClient() : prefs.client();
        if (c == null) {
            return json(503, "[{\"error\":\"" + (syncRoot
                    ? "还没配置同步目录（译文快照所在）"
                    : "坚果云账号还没配置") + "\"}]");
        }
        List<DavClient.DavEntry> entries = c.list(dir);
        StringBuilder sb = new StringBuilder("[");
        for (int i = 0; i < entries.size(); i++) {
            if (i > 0) {
                sb.append(',');
            }
            sb.append(entries.get(i).toJson());
        }
        sb.append(']');
        return json(200, sb.toString());
    }

    private WebResourceResponse fileResponse(String path, boolean syncRoot) throws Exception {
        DavClient c = syncRoot ? prefs.syncClient() : prefs.client();
        if (c == null) {
            return new WebResourceResponse("text/plain", "UTF-8", 503, "Not Configured",
                    new HashMap<String, String>(),
                    new ByteArrayInputStream(
                            (syncRoot ? "还没配置同步目录" : "坚果云账号还没配置")
                                    .getBytes(StandardCharsets.UTF_8)));
        }
        byte[] data = c.get(path);
        if (data == null) {
            return new WebResourceResponse("text/plain", "UTF-8", 404, "Not Found",
                    new HashMap<String, String>(),
                    new ByteArrayInputStream("not found".getBytes(StandardCharsets.UTF_8)));
        }
        Map<String, String> headers = new HashMap<>();
        headers.put("Cache-Control", "no-store");
        headers.put("Content-Length", String.valueOf(data.length));
        // 二进制内容 -> encoding 传 null
        return new WebResourceResponse(mimeOf(path), null, 200, "OK", headers,
                new ByteArrayInputStream(data));
    }

    private static WebResourceResponse json(int code, String body) {
        Map<String, String> headers = new HashMap<>();
        headers.put("Cache-Control", "no-store");
        return new WebResourceResponse("application/json", "UTF-8", code,
                code == 200 ? "OK" : "Error", headers,
                new ByteArrayInputStream(body.getBytes(StandardCharsets.UTF_8)));
    }

    static String mimeOf(String path) {
        String p = path.toLowerCase();
        if (p.endsWith(".pdf")) {
            return "application/pdf";
        }
        if (p.endsWith(".md")) {
            return "text/markdown";
        }
        if (p.endsWith(".json")) {
            return "application/json";
        }
        if (p.endsWith(".png")) {
            return "image/png";
        }
        if (p.endsWith(".jpg") || p.endsWith(".jpeg")) {
            return "image/jpeg";
        }
        if (p.endsWith(".svg")) {
            return "image/svg+xml";
        }
        if (p.endsWith(".txt")) {
            return "text/plain";
        }
        return "application/octet-stream";
    }
}
