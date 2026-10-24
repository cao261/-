package microbench.tablet;

import android.webkit.JavascriptInterface;
import android.webkit.WebView;

import java.io.IOException;
import java.nio.charset.StandardCharsets;

/**
 * 暴露给网页的原生桥。
 *
 * 读走 /dav/* （见 DavPathHandler），写只能走这里 —— 那边只能处理 GET。
 *
 * ⚠️ 这里刻意**不用 lambda、也不用匿名内部类**：
 *   - 匿名内部类：d8 8.2.2 读它会抛 NullPointerException
 *     ("Cannot invoke String.length() because <parameter1> is null")
 *   - lambda：javac 用 android.jar 作 bootclasspath 时找不到
 *     java.lang.invoke.LambdaMetafactory（Android 靠 D8 desugar，但
 *     javac 这一步就编译不过）
 *   具名类两条都躲得开。
 */
public class DavBridge {

    private final Prefs prefs;
    private final WebView webView;
    private final MainActivity activity;

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
        new Thread(new DavTask(this, path, text, callbackId, false)).start();
    }

    /** 删一个文件。 */
    @JavascriptInterface
    public void delete(String path, String callbackId) {
        new Thread(new DavTask(this, path, null, callbackId, true)).start();
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
