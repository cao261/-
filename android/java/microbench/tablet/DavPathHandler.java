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

    /** 两次自动探测之间的最小间隔（毫秒）。 */
    private static final long PROBE_RETRY_INTERVAL_MS = 5 * 60 * 1000L;

    private final Prefs prefs;
    /**
     * vault 前缀有没有**探到过**。
     *
     * ⚠ 探测要发好几次 PROPFIND（每次都是一次 TLS 握手）——
     *   探测失败时不能每个请求都重试一遍（会把坚果云打到限流）。
     *   探到了就记下来，后面的请求走「快路径」。
     */
    private boolean rootsResolved = false;
    /** 上次尝试探测的时刻。 */
    private long lastProbeAt = 0;
    /**
     * 上次「重探测」的时刻（前缀非空但可能配错了的那种自愈）。
     *
     * ⚠ 重探测比普通探测更贵 —— 它要递归整个云盘（好几次 PROPFIND）。
     *   所以必须节流：5 分钟内最多重探一次，
     *   否则「列一个真空的目录」也会触发全盘扫描，几秒就能把坚果云打到限流。
     */
    private long lastRedetectAt = 0;

    public DavPathHandler(Prefs prefs) {
        this.prefs = prefs;
    }

    /**
     * 确保 vault 前缀是有的。
     *
     * 没配就地探测一次（见 {@link Prefs#resolveRootsIfMissing()}）——
     * 用户没跑过原生设置页的「测试并保存」时，这是唯一的出路。
     *
     * ⚠ 探测**失败**不置位：网络抖一下不代表 vault 找不着了，
     *   过几分钟还能再试（间隔由 PROBE_RETRY_INTERVAL_MS 控制）。
     */
    private void ensureRoots() {
        if (rootsResolved) {
            return;
        }
        if (!prefs.davVaultRoot().isEmpty()) {
            rootsResolved = true;
            return;
        }
        long now = System.currentTimeMillis();
        if (now - lastProbeAt < PROBE_RETRY_INTERVAL_MS) {
            return;
        }
        lastProbeAt = now;
        try {
            prefs.resolveRootsIfMissing();
            if (!prefs.davVaultRoot().isEmpty()) {
                rootsResolved = true;
            }
        } catch (Exception e) {
            // 探测失败就当没配 —— 下面的请求会给出可读的错误
        }
    }

    /**
     * 「前缀配错了」的自愈：重新探测一次 vault 位置。
     *
     * ⚠ 什么时候需要它（区别于 {@link #ensureRoots()}）：
     *   ensureRoots 只管「前缀是**空**的」；
     *   而用户手填错了前缀、或者装过探测有 bug 的旧版 APK 时，
     *   前缀是**非空但错的** —— 探不到空值那层就不会触发。
     *   症状一模一样：列目录全是 0 条、文件全 404。
     *
     * @return 探测到新位置后、用新位置造好的客户端；没变化/失败返回 null
     */
    private DavClient tryRedetect(boolean syncRoot) {
        if (syncRoot) {
            return null;                   // 同步根不是 vault 的一部分，不参与重探测
        }
        long now = System.currentTimeMillis();
        if (now - lastRedetectAt < PROBE_RETRY_INTERVAL_MS) {
            return null;                   // 节流：5 分钟内最多重探一次
        }
        lastRedetectAt = now;
        try {
            DavClient c = prefs.redetectVaultRoot();
            if (c != null) {
                rootsResolved = true;
                lastProbeAt = now;
            }
            return c;
        } catch (Exception e) {
            return null;
        }
    }

    public WebResourceResponse handle(String path) {
        try {
            ensureRoots();
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
        // ⚠ 「0 个条目」有两种可能：目录真的是空的 / 前缀配错了。
        //   WebDAV 对**不存在的目录**回 207 + 空集合（不报 404），
        //   所以这里没法靠状态码区分 —— 只能靠「重探一次位置对不对」。
        if (entries.isEmpty()) {
            DavClient fixed = tryRedetect(syncRoot);
            if (fixed != null) {
                entries = fixed.list(dir);
            }
        }
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
        // ⚠ 2026-10-27 审计 M-1：原来 `byte[] data = c.get(path)` ——
        //   整份文件先进内存（上限 128 MB）→ 平板 OOM。
        //   WebResourceResponse 本来就吃 InputStream → 直接把连接流给它，
        //   Java 侧一个字节都不攒。
        DavClient.DavStream stream = c.openFile(path);
        if (stream == null) {
            // ⚠ 404 有两种可能：文件真的没了 / 前缀配错了。
            //   后者正是用户看到的「24 篇原文全 404」——
            //   重探一次位置，位置对上了就能真下到文件。
            DavClient fixed = tryRedetect(syncRoot);
            if (fixed != null) {
                stream = fixed.openFile(path);
            }
        }
        if (stream == null) {
            return new WebResourceResponse("text/plain", "UTF-8", 404, "Not Found",
                    new HashMap<String, String>(),
                    new ByteArrayInputStream("not found".getBytes(StandardCharsets.UTF_8)));
        }
        Map<String, String> headers = new HashMap<>();
        headers.put("Cache-Control", "no-store");
        long len = stream.length();
        if (len > 0) {
            headers.put("Content-Length", String.valueOf(len));
        }
        // 二进制内容 -> encoding 传 null
        return new WebResourceResponse(mimeOf(path), null, 200, "OK", headers, stream);
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
