package microbench.tablet;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.net.http.SslError;
import android.os.Bundle;
import android.os.Environment;
import android.util.Base64;
import android.webkit.ConsoleMessage;
import android.webkit.DownloadListener;
import android.webkit.JavascriptInterface;
import android.webkit.SslErrorHandler;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import java.io.ByteArrayInputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;

/**
 * 平板工作台的原生外壳。
 *
 * 关键设计：页面跑在 **https://appassets.androidplatform.net** 这个源上，
 * 而不是 file://。
 *   - file:// 是 opaque origin：IndexedDB 的行为诡异、Service Worker 不可用、
 *     Blob URL 的 Worker 也不稳 —— 而 PDF.js 的 worker 正是靠 Blob URL 起的。
 *   - https 源下这些全都正常，页面代码不用为「在浏览器里跑」和「在 APK 里跑」
 *     分叉。
 *
 * 这个域名的请求全部在本地拦截（assets 目录 + /dav/* 走坚果云 WebDAV），
 * 不会真的发到网络。SSL 错误因此是预期的 —— 只对这一个域名放行。
 *
 * 注意：本工程**刻意不依赖 androidx**，这样可以不用 Gradle 手工打包 APK。
 */
public class MainActivity extends Activity {

    private static final String APP_DOMAIN = "appassets.androidplatform.net";
    private static final String START_URL =
            "https://" + APP_DOMAIN + "/assets/index.html";

    /** <input type="file"> 的请求码。跟设置页的 REQ 区分开。 */
    private static final int REQ_FILE = 1002;

    private WebView webView;
    private Prefs prefs;
    private DavPathHandler davHandler;
    private DavBridge bridge;
    /** 文件选择器回调（网页里的 <input type="file">）。 */
    private ValueCallback<Uri[]> filePathCallback;

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        prefs = new Prefs(this);
        davHandler = new DavPathHandler(prefs);

        webView = new WebView(this);
        WebSettings s = webView.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        // 本应用的资源**全部**走 shouldInterceptRequest 从 assets 里发 ——
        // 没有任何地方需要 file:// 或 content:// 的直接读取权限。
        // 关掉它们，页面就没法偷偷摸本地文件。
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setSupportZoom(false);          // 页面自己管缩放
        s.setUseWideViewPort(true);
        s.setLoadWithOverviewMode(false);
        s.setCacheMode(WebSettings.LOAD_NO_CACHE);

        webView.setWebViewClient(new LocalClient());

        bridge = new DavBridge(prefs, webView, this);
        webView.addJavascriptInterface(bridge, "MicroBench");
        // 下载落盘用的桥（blob: 下载由页面把内容读出来交回来）。
        webView.addJavascriptInterface(new DownloadBridge(this), "MicroBenchDownload");
        // 导出 vault 包 / 变更包走 <a download> → 没有 DownloadListener
        // 的话点「导出」在 APK 里**什么都不发生**（审计 M-1）。
        webView.setDownloadListener(new DownloadHandler(this));

        // 网页里的 <input type="file">（导入 vault 包）需要 WebChromeClient
        // 配合 —— 没有它, 点那个按钮会**什么都不发生**。
        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView view,
                                             ValueCallback<Uri[]> callback,
                                             FileChooserParams params) {
                // 必须先把旧的回调清掉 —— 不清的话 <input> 会永久卡住,
                // 之后再点导入永远没反应。
                if (filePathCallback != null) {
                    filePathCallback.onReceiveValue(null);
                }
                filePathCallback = callback;
                try {
                    startActivityForResult(params.createIntent(), REQ_FILE);
                    return true;
                } catch (Exception e) {
                    filePathCallback = null;
                    return false;
                }
            }

            @Override
            public boolean onConsoleMessage(ConsoleMessage cm) {
                // 默认实现会把 console.log 全部丢掉 —— 真机调试就没有探针了。
                // 转进 logcat, 至少还能看。
                android.util.Log.d("MicroBench",
                        cm.message() + " @" + cm.sourceId() + ":" + cm.lineNumber());
                return true;
            }
        });

        setContentView(webView);
        webView.loadUrl(START_URL);
    }

    /**
     * 物理返回键。
     * 优先级: 关掉 PDF 阅读器 -> 关掉设置弹窗 -> 网页后退 -> 退出应用。
     * 少了这一段, 在 PDF 阅读器里按返回会**直接退出应用**。
     */
    @Override
    public void onBackPressed() {
        if (webView == null) {
            super.onBackPressed();
            return;
        }
        String js = "(function(){"
                + "var r=false;"
                + "if (document.getElementById('pdf-reader')"
                + "    && !document.getElementById('pdf-reader').hidden) {"
                + "  document.getElementById('pdf-reader').hidden=true; r=true; }"
                + "else if (document.getElementById('pdf-lib')"
                + "    && !document.getElementById('pdf-lib').hidden) {"
                + "  document.getElementById('pdf-lib').hidden=true; r=true; }"
                + "else if (document.getElementById('modal-settings')"
                + "    && !document.getElementById('modal-settings').hidden) {"
                + "  document.getElementById('modal-settings').hidden=true; r=true; }"
                + "else if (document.getElementById('modal-new')"
                + "    && !document.getElementById('modal-new').hidden) {"
                + "  document.getElementById('modal-new').hidden=true; r=true; }"
                + "else { var d=document.querySelector('.tb-shell');"
                + "  if (d && d.classList.contains('side-open')) {"
                + "    d.classList.remove('side-open'); r=true; } }"
                + "return r; })()";
        webView.evaluateJavascript(js, new ValueCallback<String>() {
            @Override
            public void onReceiveValue(String value) {
                if (!"true".equals(value)) {
                    if (webView.canGoBack()) {
                        webView.goBack();
                    } else {
                        MainActivity.super.onBackPressed();
                    }
                }
            }
        });
    }

    /**
     * 具名内部类而不是匿名类 —— d8 8.2.2 读匿名内部类会抛
     * NullPointerException（"Cannot invoke String.length()"）。
     */
    private class LocalClient extends WebViewClient {

        /**
         * 离开 appassets 源的导航**一律拦掉**。
         *
         * ⚠⚠ 2026-10-27 审计 C-1（最高优先级）：
         *   `addJavascriptInterface(bridge, "MicroBench")` 注入的原生桥
         *   在**当前页面**里是全局对象 —— 页面里任何脚本都能调
         *   delete("01_Literature/x.pdf") 删原文、put(path, text) 往 vault 写内容
         *   （会同步回电脑端，污染用户笔记）、httpPost(url, body, auth)
         *   把平板当跳板去打用户内网的 https。
         *
         *   触发路径：笔记 / AI 答案里的外链 `[点我](https://evil)` →
         *   <a> 没有 target、WebView 又没有 shouldOverrideUrlLoading →
         *   **顶层导航**到外域 → 恶意页拿到 window.MicroBench。
         *
         * → 只有 appassets 源（自己人）留在 WebView 里，
         *   其余一律交给系统浏览器（Intent.ACTION_VIEW），**绝不**在
         *   WebView 里加载外域页面。
         */
        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            if (request == null || request.getUrl() == null) {
                return false;
            }
            Uri uri = request.getUrl();
            if (isAppSource(uri)) {
                return false;                 // 自己人，放行
            }
            if (request.isForMainFrame()) {
                openExternally(uri);          // 主框架 → 系统浏览器
            }
            // ⚠ iframe 也要拦：addJavascriptInterface 注入的对象
            //   在**每一个** frame 里都有（跨域 iframe 拿得到自己窗口里的那份）。
            //   不拦的话，外域 iframe 就是一条绕开主框架限制的路。
            return true;
        }

        @Override
        public WebResourceResponse shouldInterceptRequest(WebView view,
                                                          WebResourceRequest request) {
            Uri uri = request.getUrl();
            if (uri == null || !APP_DOMAIN.equals(uri.getHost())) {
                return null;
            }
            String path = uri.getPath();
            if (path == null) {
                path = "/";
            }
            try {
                if (path.startsWith("/dav/")) {
                    return davHandler.handle(path);
                }
                if (path.startsWith("/assets/")) {
                    return serveAsset(path.substring("/assets/".length()));
                }
            } catch (Exception e) {
                return text(500, "ERR " + e.getMessage());
            }
            return text(404, "not found");
        }

        @Override
        public void onReceivedSslError(WebView view, SslErrorHandler handler,
                                       SslError error) {
            // 只对本地那个域名放行：请求在 shouldInterceptRequest 里就被
            // 本地接管了，证书本来就不存在。
            // SslError.getUrl() 返回的是 String。
            String url = error.getUrl();
            Uri u = url == null ? null : Uri.parse(url);
            // ⚠ 只放行「证书不受信任」这一种 —— 其它错误类型一律取消。
            //   全放行的话，万一有请求漏过拦截器（域名写错了一点点），
            //   就会带着 vault 的源码和凭证发到真实互联网上去。
            if (u != null && APP_DOMAIN.equals(u.getHost())
                    && error.getPrimaryError() == SslError.SSL_UNTRUSTED) {
                handler.proceed();
            } else {
                handler.cancel();
            }
        }
    }

    // ------------------------------------------------------------------
    // 外链处理（审计 C-1）
    // ------------------------------------------------------------------

    /** 这个 URI 是不是「自己人」（可以留在 WebView 里）。 */
    private static boolean isAppSource(Uri uri) {
        if (uri == null) {
            return false;
        }
        String host = uri.getHost();
        if (host != null && APP_DOMAIN.equalsIgnoreCase(host)) {
            return true;
        }
        // 只放行 about:blank（空白页没有脚本，也不带任何来源）。
        //
        // ⚠ **绝不放行 data:** —— data: 页面是 opaque origin，
        //   而 addJavascriptInterface 注入的对象在**每一个被加载的页面**里都有，
        //   放行它等于把原生桥交给一个来路不明的页面。
        return "about:blank".equalsIgnoreCase(uri.toString());
    }

    /** 把外链交给系统浏览器（**绝不在** WebView 里打开）。 */
    private void openExternally(Uri uri) {
        try {
            Intent i = new Intent(Intent.ACTION_VIEW, uri);
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            startActivity(i);
        } catch (Exception e) {
            // 没有浏览器 / 打不开 → 告诉用户，别静默失败。
            toast("打不开这个链接：" + uri);
        }
    }

    // ------------------------------------------------------------------
    // 下载（导出 vault 包 / 变更包，审计 M-1）
    // ------------------------------------------------------------------

    /**
     * 把 blob:/data: 的下载内容从页面取回来。
     *
     * ⚠ 为什么必须由页面取：blob: URL 只有**创建它的那个页面**能读
     *   （原生侧拿到这个 URL 也没用 —— 它不是文件，是页面内存里的对象）。
     *   → 注入一小段 JS，用 fetch + FileReader 把它变成 base64 交回来。
     *
     * ⚠ 分块传：一次 Binder 事务大约 1 MB 上限，
     *   整个 base64 一次塞过去会 TransactionTooLargeException。
     *   → 每块 256 KB（4 的倍数，不破坏 base64 编码单元）。
     */
    private void fetchBlobAsBase64(String url, String contentDisposition, String mimetype) {
        if (webView == null) {
            return;
        }
        final String js =
                "(function(){try{"
                + "fetch(" + jsString(url) + ").then(function(r){return r.blob();})"
                + ".then(function(b){return new Promise(function(res,rej){"
                + "var fr=new FileReader();fr.onload=function(){res(fr.result);};"
                + "fr.onerror=rej;fr.readAsDataURL(b);});})"
                + ".then(function(d){var i=d.indexOf(',');"
                + "if(i<0||!window.MicroBenchDownload){return;}"
                + "var b64=d.substring(i+1),CH=262144,n=Math.ceil(b64.length/CH);"
                + "window.MicroBenchDownload.begin(" + jsString(contentDisposition) + ","
                + jsString(mimetype) + ",n);"
                + "for(var k=0;k<n;k++){"
                + "window.MicroBenchDownload.chunk(b64.substring(k*CH,(k+1)*CH));}"
                + "window.MicroBenchDownload.end();})"
                + ".catch(function(e){console.error('mb download failed',e);});"
                + "}catch(e){console.error('mb download failed',e);}})();";
        webView.evaluateJavascript(js, null);
    }

    /** 把 base64 内容落盘，并给用户一个看得见的反馈。 */
    private void writeDownload(String base64, String contentDisposition, String mimetype) {
        try {
            if (base64 == null || base64.isEmpty()) {
                toast("下载内容为空");
                return;
            }
            byte[] bytes = Base64.decode(base64, Base64.DEFAULT);
            File dir = getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS);
            if (dir == null) {
                dir = new File(getFilesDir(), "Downloads");
            }
            if (!dir.exists() && !dir.mkdirs()) {
                toast("建不了下载目录：" + dir.getAbsolutePath());
                return;
            }
            File out = new File(dir, safeFileName(contentDisposition));
            FileOutputStream fos = new FileOutputStream(out);
            try {
                fos.write(bytes);
            } finally {
                fos.close();
            }
            toast("已保存：" + out.getAbsolutePath());
        } catch (Exception e) {
            toast("保存失败：" + (e.getMessage() == null ? e.toString() : e.getMessage()));
        }
    }

    /** 从 Content-Disposition 里取出文件名（并去掉路径成分）。 */
    private static String safeFileName(String disposition) {
        String name = "";
        if (disposition != null) {
            String d = disposition.trim();
            int i = d.toLowerCase().indexOf("filename=");
            if (i >= 0) {
                name = d.substring(i + "filename=".length()).trim();
                if (name.startsWith("\"")) {
                    int j = name.indexOf('"', 1);
                    name = j > 0 ? name.substring(1, j) : name.substring(1);
                } else {
                    int j = name.indexOf(';');
                    if (j > 0) {
                        name = name.substring(0, j);
                    }
                }
            }
        }
        name = name.replace('\\', '/');
        int slash = name.lastIndexOf('/');
        if (slash >= 0) {
            name = name.substring(slash + 1);
        }
        name = name.trim();
        if (name.isEmpty() || ".".equals(name) || "..".equals(name)) {
            name = "microbench-导出-" + System.currentTimeMillis() + ".bin";
        }
        return name;
    }

    /** 把字符串转成 JS 里的字符串字面量（含引号）。 */
    private static String jsString(String s) {
        if (s == null) {
            return "\"\"";
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

    private void toast(String msg) {
        Toast.makeText(this, msg, Toast.LENGTH_LONG).show();
    }

    /** 打开原生设置页（填坚果云账号）。 */
    void openSettings() {
        try {
            // 必须是 startActivityForResult —— 用 startActivity 的话
            // onActivityResult 永远不会触发, 设置页保存后页面不知道,
            // 状态就一直显示「未连接坚果云」(踩过这个坑)。
            startActivityForResult(new Intent(this, SettingsActivity.class),
                    SettingsActivity.REQ);
        } catch (Exception e) {
            // 静默失败的话用户只会觉得「点了没反应」。
            Toast.makeText(this, "打不开设置页：" + (e.getMessage() == null
                    ? e.toString() : e.getMessage()), Toast.LENGTH_LONG).show();
        }
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        super.onSaveInstanceState(outState);
        // 进程被系统杀掉再回来时，WebView 的状态（当前笔记/滚动位置）
        // 靠这个恢复 —— 不然用户得从头再翻一遍。
        if (webView != null) {
            webView.saveState(outState);
        }
    }

    @Override
    protected void onRestoreInstanceState(Bundle savedInstanceState) {
        super.onRestoreInstanceState(savedInstanceState);
        if (webView != null) {
            webView.restoreState(savedInstanceState);
        }
    }

    @Override
    protected void onPause() {
        // 进后台后页面的定时器和同步线程还在跑 —— 平板切几个应用就发烫。
        if (webView != null) {
            webView.onPause();
            webView.pauseTimers();
        }
        super.onPause();
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (webView != null) {
            webView.resumeTimers();
            webView.onResume();
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);

        // ① 网页里的 <input type="file">（导入 vault 包）选完文件要回传给网页。
        //    不回传的话, 网页的 FileReader 永远等不到文件 —— 表现为
        //    「点了导入，什么也没发生」。
        if (requestCode == REQ_FILE) {
            if (filePathCallback != null) {
                Uri[] uris = null;
                if (resultCode == RESULT_OK && data != null) {
                    try {
                        uris = WebChromeClient.FileChooserParams.parseResult(resultCode, data);
                    } catch (Exception e) {
                        uris = null;
                    }
                }
                filePathCallback.onReceiveValue(uris);
                filePathCallback = null;
            }
            return;
        }

        // ② 原生设置页保存完凭证回来 —— 通知页面重新探测同步状态并刷新界面。
        if (requestCode == SettingsActivity.REQ && resultCode == RESULT_OK && webView != null) {
            // 不要 location.reload() —— 那会丢掉页面里已经有的状态。
            webView.evaluateJavascript(
                    "window.__mbRefreshSync && window.__mbRefreshSync();", null);
        }
    }

    @Override
    protected void onDestroy() {
        // WebView 不销毁的话, 每次退到后台再进来都会攒一个实例 ——
        // 平板内存不大, 攒几次就开始卡。
        if (webView != null) {
            // 先摘掉 JS 桥, 不然后台线程回调会打到一个已经销毁的 WebView 上。
            webView.removeJavascriptInterface("MicroBench");
            webView.removeJavascriptInterface("MicroBenchDownload");
            webView.stopLoading();
            webView.setWebChromeClient(null);
            webView.destroy();
            webView = null;
        }
        super.onDestroy();
    }

    // ------------------------------------------------------------------
    // 具名内部类（不用 lambda、不用匿名内部类 —— 见 DavBridge 的注释）
    // ------------------------------------------------------------------

    /**
     * 网页里的下载请求。
     *
     * ⚠ 导出 vault 包 / 变更包走的是 `URL.createObjectURL` + `<a download>`，
     *   没有 DownloadListener 的话，在 APK 里点「导出」**什么都不发生**
     *   （审计 M-1）。
     */
    private static class DownloadHandler implements DownloadListener {

        private final MainActivity act;

        DownloadHandler(MainActivity act) {
            this.act = act;
        }

        @Override
        public void onDownloadStart(String url, String userAgent, String contentDisposition,
                                    String mimetype, long contentLength) {
            if (url == null || url.trim().isEmpty()) {
                return;
            }
            String u = url.trim();
            if (u.startsWith("blob:") || u.startsWith("data:")) {
                // blob:/data: 只有页面自己读得出来 → 让页面交回来。
                act.fetchBlobAsBase64(u, contentDisposition, mimetype);
                return;
            }
            // http(s) → 交给系统（浏览器 / 下载器）。
            act.openExternally(Uri.parse(u));
        }
    }

    /**
     * 下载落盘的 JS 桥（分块接收，避免 Binder 1 MB 事务上限）。
     */
    public static class DownloadBridge {

        private final MainActivity act;
        private final StringBuilder buf = new StringBuilder();
        private String disposition = "";
        private String mime = "";

        DownloadBridge(MainActivity act) {
            this.act = act;
        }

        @JavascriptInterface
        public void begin(String contentDisposition, String mimetype, int totalChunks) {
            buf.setLength(0);
            disposition = contentDisposition == null ? "" : contentDisposition;
            mime = mimetype == null ? "" : mimetype;
        }

        @JavascriptInterface
        public void chunk(String part) {
            if (part != null) {
                buf.append(part);
            }
        }

        @JavascriptInterface
        public void end() {
            act.writeDownload(buf.toString(), disposition, mime);
            buf.setLength(0);
        }
    }

    // ------------------------------------------------------------------
    // 本地资源
    // ------------------------------------------------------------------

    private WebResourceResponse serveAsset(String name) {
        if (name == null || name.isEmpty() || name.contains("..")) {
            return text(404, "not found");
        }
        try {
            InputStream in = getAssets().open(name);
            Map<String, String> headers = new HashMap<>();
            headers.put("Cache-Control", "no-store");
            return new WebResourceResponse(mimeOf(name), null, 200, "OK", headers, in);
        } catch (IOException e) {
            return text(404, "asset not found: " + name);
        }
    }

    private static WebResourceResponse text(int code, String body) {
        Map<String, String> headers = new HashMap<>();
        headers.put("Cache-Control", "no-store");
        return new WebResourceResponse("text/plain", "UTF-8", code,
                code == 200 ? "OK" : "Error", headers,
                new ByteArrayInputStream(body.getBytes(StandardCharsets.UTF_8)));
    }

    static String mimeOf(String path) {
        String p = path.toLowerCase();
        if (p.endsWith(".html") || p.endsWith(".htm")) {
            return "text/html";
        }
        if (p.endsWith(".js")) {
            return "application/javascript";
        }
        if (p.endsWith(".css")) {
            return "text/css";
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
        if (p.endsWith(".woff2")) {
            return "font/woff2";
        }
        if (p.endsWith(".md")) {
            return "text/markdown";
        }
        if (p.endsWith(".pdf")) {
            return "application/pdf";
        }
        return "application/octet-stream";
    }
}
