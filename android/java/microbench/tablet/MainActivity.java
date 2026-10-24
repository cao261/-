package microbench.tablet;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.net.http.SslError;
import android.os.Bundle;
import android.webkit.ConsoleMessage;
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
            webView.stopLoading();
            webView.setWebChromeClient(null);
            webView.destroy();
            webView = null;
        }
        super.onDestroy();
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
