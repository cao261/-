package microbench.tablet;

import android.webkit.JavascriptInterface;
import android.webkit.WebView;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 暴露给网页的原生桥。
 *
 * 读走 /dav/* （见 DavPathHandler），写只能走这里 —— 那边只能处理 GET。
 *
 * 另外还代理一次 AI 请求（httpPost）—— APK 里页面的 origin 是
 * https://appassets.androidplatform.net，外部 API 不会回这个 origin 的
 * Access-Control-Allow-Origin，fetch 必被 CORS 打死。走原生就没有跨域问题。
 *
 * ⚠ 这里刻意**不用 lambda、也不用匿名内部类**：
 *   - 匿名内部类：d8 8.2.2 读它会抛 NullPointerException
 *     ("Cannot invoke String.length() because <parameter1> is null")
 *   - lambda：javac 用 android.jar 作 bootclasspath 时找不到
 *     java.lang.invoke.LambdaMetafactory（Android 靠 D8 desugar，但
 *     javac 这一步就编译不过）
 *   具名类两条都躲得开。
 */
public class DavBridge {

    /** 一次原生调用的最长等待时间（毫秒）。 */
    private static final int NATIVE_TIMEOUT_MS = 120_000;

    /**
     * AI 请求（httpPost）的读超时（毫秒）。
     *
     * ⚠ 2026-10-27 独立审查揪出来的坑：AI 答疑的真实时限是 **300s**
     *   （平板端 AI_QA_TIMEOUT_MS，与电脑端 QA_TIMEOUT 一致）——
     *   而这里原来跟 WebDAV 同步共用 120s 的读超时 →
     *   思考型模型答长问题（电脑端实测 max_tokens=16000 就要 86.2s）
     *   会在 120s 被这里**先**掐掉，错误信息还是内部的 SocketTimeoutException。
     *   → AI 请求必须给得比 300s 更宽（留出超时判定余量）。
     */
    private static final int AI_READ_TIMEOUT_MS = 360_000;

    private final Prefs prefs;
    private final WebView webView;
    private final MainActivity activity;

    /**
     * **DAV 写操作**的线程池：单线程。
     *
     * 以前每写一个文件就 new Thread —— 批量推 24 篇笔记就是 24 个线程
     * 各自建一条 TLS 连接，机器直接卡住。
     *
     * ⚠ 必须**单线程**：写操作要保序（同一个文件覆盖写不能乱序，
     *   否则「先写新内容再写旧内容」会把新内容盖掉）。
     */
    private static final ExecutorService POOL_DAV = Executors.newSingleThreadExecutor();

    /**
     * **AI 请求**的线程池：与 DAV **分开**，最多 2 个并发。
     *
     * ⚠ 2026-10-27 审计 H-1：原来 AI 代理与云盘写入共用**同一个**单线程池
     *   → AI 答疑最长跑 360 s，期间用户点「保存笔记」「写批注」全排在后面
     *   → 前端 150 s 超时先到 → 只弹一句「已存本地，但写回云盘失败」。
     *   → 拆成两个池：AI 再慢也不挡云盘写入。
     */
    private static final ExecutorService POOL_AI = Executors.newFixedThreadPool(2);

    public DavBridge(Prefs prefs, WebView webView, MainActivity activity) {
        this.prefs = prefs;
        this.webView = webView;
        this.activity = activity;
    }

    /** 坚果云账号配好了没。 */
    @JavascriptInterface
    public boolean davConfigured() {
        return prefs.davConfigured();
    }

    /**
     * 真实状态：账号配了没、vault 在云盘的哪个位置、同步根在哪。
     *
     * ⚠ 网页光看「账号密码齐了没」会得出「已连接」的结论 ——
     *   但**账号齐了 ≠ 路径对了**。少了 vault 前缀，列目录返回空、
     *   下载全部 404，而界面显示「已连接」（2026-10-25 用户真机故障）。
     * → 网页要能问出「vault 到底在哪」，才能把真话告诉用户。
     */
    @JavascriptInterface
    public String davInfo() {
        return "{\"configured\":" + (prefs.davConfigured() ? "true" : "false")
                + ",\"vaultRoot\":" + jsonEscape(prefs.davVaultRoot())
                + ",\"syncRoot\":" + jsonEscape(prefs.davSyncRoot())
                + "}";
    }

    /** 打开原生设置页（填坚果云账号 / 应用密码）。 */
    @JavascriptInterface
    public void openSettings() {
        if (activity != null) {
            activity.runOnUiThread(new OpenSettingsTask(activity));
        }
    }

    /** 写一个文件到云盘。 */
    @JavascriptInterface
    public void put(String path, String text, String callbackId) {
        if (path == null || path.trim().isEmpty()) {
            post(callbackId, false, "{\"ok\":false,\"error\":\"路径为空\"}");
            return;
        }
        POOL_DAV.execute(new DavTask(this, path, text == null ? "" : text, callbackId, false));
    }

    /** 删一个文件。 */
    @JavascriptInterface
    public void delete(String path, String callbackId) {
        if (path == null || path.trim().isEmpty()) {
            post(callbackId, false, "{\"ok\":false,\"error\":\"路径为空\"}");
            return;
        }
        POOL_DAV.execute(new DavTask(this, path, null, callbackId, true));
    }

    /**
     * 写到**同步根**（译文快照 / 批注所在），不是 vault。
     *
     * ⚠ 平板上手写标注要存到共享目录，跟电脑端看到的是同一份 ——
     *   否则平板画的笔记电脑看不到，等于白画。
     */
    @JavascriptInterface
    public void putSync(String path, String text, String callbackId) {
        if (path == null || path.trim().isEmpty()) {
            post(callbackId, false, "{\"ok\":false,\"error\":\"路径为空\"}");
            return;
        }
        POOL_DAV.execute(new SyncPutTask(this, path, text == null ? "" : text, callbackId));
    }

    /**
     * 登记一个「用户自己配置的 AI 网关」主机。
     *
     * ⚠ 为什么要它：AI 网关地址存在**网页的 IndexedDB** 里，原生侧读不到；
     *   而 httpPost 的目标主机必须是白名单（审计 C-1）。
     *   → 页面在用户保存 AI 设置时调用这个方法，把主机名登记进原生白名单。
     *
     * ⚠ 只收公网 DNS 名（IP 字面量 / 内网名一律拒绝）——
     *   否则「把平板当跳板打用户内网」这条路就又通了。
     *
     * @param base 网关地址（形如 https://api.minimaxi.com/v1）
     * @return 登记成功返回 true
     */
    @JavascriptInterface
    public boolean allowAiHost(String base) {
        return prefs.allowAiHost(base);
    }

    /**
     * 代理一次 HTTP POST（给 AI 接口用）。
     *
     * ⚠ 2026-10-27 审计 C-1：目标主机必须是**白名单**里的 ——
     *   原来只校验「https://」前缀 = 开放代理：
     *   页面里的任何脚本都能让平板代它发请求（尤其能拿平板当跳板
     *   去打用户内网的 https 服务）。
     *   → 白名单见 {@link Prefs#DEFAULT_ALLOWED_HOSTS}，
     *     用户自己的网关由 {@link #allowAiHost(String)} 登记。
     *
     * @param auth 形如 "Bearer xxx"，为空就不加 Authorization 头
     */
    @JavascriptInterface
    public void httpPost(String url, String body, String auth, String callbackId) {
        String host = Prefs.hostOf(url);
        if (host == null) {
            post(callbackId, false, "{\"ok\":false,\"error\":\"只允许 https\"}");
            return;
        }
        if (!prefs.isAiHostAllowed(host)) {
            post(callbackId, false, "{\"ok\":false,\"error\":\"网关域名不在白名单："
                    + jsonEscape(host)
                    + "（平板版需要在「设置」里保存一次 AI 网关地址）\"}");
            return;
        }
        POOL_AI.execute(new HttpTask(this, url, body == null ? "" : body,
                auth == null ? "" : auth, callbackId));
    }

    void post(String callbackId, boolean ok, String payload) {
        if (webView == null || callbackId == null || callbackId.isEmpty()) {
            return;
        }
        final String js = "window.__mbNativeResolve && window.__mbNativeResolve("
                + jsonEscape(callbackId) + "," + (ok ? "true" : "false") + ","
                + payload + ");";
        webView.post(new EvalTask(webView, js));
    }

    static String jsonEscape(String s) {
        if (s == null) {
            return "null";
        }
        StringBuilder sb = new StringBuilder("\"");
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"': sb.append("\\\""); break;
                case '\\': sb.append("\\\\"); break;
                case '\n': sb.append("\\n"); break;
                case '\r': sb.append("\\r"); break;
                case '\t': sb.append("\\t"); break;
                default:
                    if (c < 0x20) {
                        sb.append(String.format("\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
            }
        }
        sb.append('"');
        return sb.toString();
    }

    // ------------------------------------------------------------------
    // 具名任务类
    // ------------------------------------------------------------------

    /** 在云盘上写/删一个文件, 然后回调网页。 */
    private static class DavTask implements Runnable {
        private final DavBridge bridge;
        private final String path;
        private final String text;
        private final String callbackId;
        private final boolean isDelete;

        DavTask(DavBridge bridge, String path, String text, String callbackId,
               boolean isDelete) {
            this.bridge = bridge;
            this.path = path;
            this.text = text;
            this.callbackId = callbackId;
            this.isDelete = isDelete;
        }

        @Override
        public void run() {
            String result;
            boolean ok;
            try {
                DavClient c = bridge.prefs.client();
                if (c == null) {
                    throw new IOException("坚果云账号还没配置");
                }
                if (isDelete) {
                    c.delete(path);
                } else {
                    c.put(path, text.getBytes(StandardCharsets.UTF_8));
                }
                result = "{\"ok\":true}";
                ok = true;
            } catch (Exception e) {
                String msg = e.getMessage() == null ? e.toString() : e.getMessage();
                result = "{\"ok\":false,\"error\":" + jsonEscape(msg) + "}";
                ok = false;
            }
            bridge.post(callbackId, ok, result);
        }
    }

    /** 写到同步根（译文快照 / 批注所在）。 */
    private static class SyncPutTask implements Runnable {
        private final DavBridge bridge;
        private final String path;
        private final String text;
        private final String callbackId;

        SyncPutTask(DavBridge bridge, String path, String text, String callbackId) {
            this.bridge = bridge;
            this.path = path;
            this.text = text;
            this.callbackId = callbackId;
        }

        @Override
        public void run() {
            boolean ok;
            String result;
            try {
                DavClient c = bridge.prefs.syncClient();
                if (c == null) {
                    throw new IOException("还没配置同步目录（点设置里的「配置坚果云」重测一次）");
                }
                c.put(path, text.getBytes(StandardCharsets.UTF_8));
                ok = true;
                result = "{\"ok\":true}";
            } catch (Exception e) {
                String msg = e.getMessage() == null ? e.toString() : e.getMessage();
                ok = false;
                result = "{\"ok\":false,\"error\":" + jsonEscape(msg) + "}";
            }
            bridge.post(callbackId, ok, result);
        }
    }

    /** 代理一次 AI 的 HTTP POST。 */
    private static class HttpTask implements Runnable {
        private final DavBridge bridge;
        private final String url;
        private final String body;
        private final String auth;
        private final String callbackId;

        HttpTask(DavBridge bridge, String url, String body, String auth,
                 String callbackId) {
            this.bridge = bridge;
            this.url = url;
            this.body = body;
            this.auth = auth;
            this.callbackId = callbackId;
        }

        @Override
        public void run() {
            boolean ok;
            String result;
            try {
                HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
                try {
                    c.setRequestMethod("POST");
                    c.setConnectTimeout(30_000);
                    // AI 请求要给足时间（思考型模型可能想 1~2 分钟），
                    // 不能跟 WebDAV 同步用同一个短读超时。
                    c.setReadTimeout(AI_READ_TIMEOUT_MS);
                    c.setDoOutput(true);
                    c.setRequestProperty("Content-Type", "application/json; charset=utf-8");
                    c.setRequestProperty("Accept", "text/event-stream, application/json");
                    if (!auth.isEmpty()) {
                        c.setRequestProperty("Authorization", auth);
                    }
                    byte[] data = body.getBytes(StandardCharsets.UTF_8);
                    c.setFixedLengthStreamingMode(data.length);
                    try (OutputStream os = c.getOutputStream()) {
                        os.write(data);
                    }
                    int code = c.getResponseCode();
                    InputStream in = (code >= 200 && code < 300)
                            ? c.getInputStream() : c.getErrorStream();
                    String text = in == null ? "" : new String(readAll(in), StandardCharsets.UTF_8);
                    if (code >= 200 && code < 300) {
                        ok = true;
                        result = "{\"ok\":true,\"status\":" + code + ",\"body\":"
                                + jsonEscape(text) + "}";
                    } else {
                        ok = false;
                        result = "{\"ok\":false,\"status\":" + code + ",\"error\":"
                                + jsonEscape(text) + "}";
                    }
                } finally {
                    c.disconnect();
                }
            } catch (Exception e) {
                String msg = e.getMessage() == null ? e.toString() : e.getMessage();
                ok = false;
                result = "{\"ok\":false,\"error\":" + jsonEscape(msg) + "}";
            }
            bridge.post(callbackId, ok, result);
        }

        private static byte[] readAll(InputStream in) throws IOException {
            ByteArrayOutputStream bo = new ByteArrayOutputStream(16 * 1024);
            byte[] buf = new byte[16 * 1024];
            int n;
            while ((n = in.read(buf)) > 0) {
                bo.write(buf, 0, n);
            }
            return bo.toByteArray();
        }
    }

    /** 在主线程上执行一小段 JS。 */
    private static class EvalTask implements Runnable {
        private final WebView view;
        private final String js;

        EvalTask(WebView view, String js) {
            this.view = view;
            this.js = js;
        }

        @Override
        public void run() {
            view.evaluateJavascript(js, null);
        }
    }

    /** 打开原生设置页。 */
    private static class OpenSettingsTask implements Runnable {
        private final MainActivity activity;

        OpenSettingsTask(MainActivity activity) {
            this.activity = activity;
        }

        @Override
        public void run() {
            activity.openSettings();
        }
    }
}
