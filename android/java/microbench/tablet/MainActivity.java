package microbench.tablet;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.net.http.SslError;
import android.os.Bundle;
import android.webkit.SslErrorHandler;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

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

    private WebView webView;
    private Prefs prefs;
    private DavPathHandler davHandler;
    private DavBridge bridge;

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
        s.setAllowFileAccess(true);
        s.setAllowContentAccess(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setSupportZoom(false);          // 页面自己管缩放
        s.setUseWideViewPort(true);
        s.setLoadWithOverviewMode(false);
        s.setCacheMode(WebSettings.LOAD_NO_CACHE);

        webView.setWebViewClient(new LocalClient());

        bridge = new DavBridge(prefs, webView, this);
        webView.addJavascriptInterface(bridge, "MicroBench");

        setContentView(webView);
        webView.loadUrl(START_URL);
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
            if (u != null && APP_DOMAIN.equals(u.getHost())) {
                handler.proceed();
            } else {
                handler.cancel();
            }
        }
    }

    /** 打开原生设置页（填坚果云账号）。 */
    void openSettings() {
        try {
            startActivity(new Intent(this, SettingsActivity.class));
        } catch (Exception ignored) {
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == SettingsActivity.REQ && resultCode == RESULT_OK && webView != null) {
            // 设置变了 -> 重新加载页面, 让它重新探测「坚果云配好了没」
            webView.evaluateJavascript("location.reload();", null);
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
