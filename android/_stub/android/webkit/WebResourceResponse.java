package android.webkit;

import java.io.InputStream;
import java.util.Map;

/**
 * 测试用的 android.webkit.WebResourceResponse 桩。
 *
 * 字段全部 public，测试可以直接读状态码和响应体。
 * 真实 Android 里这些是 getter，这里为了测试简单直接暴露字段。
 */
public class WebResourceResponse {

    public final String mimeType;
    public final String encoding;
    public final int statusCode;
    public final String reasonPhrase;
    public final Map<String, String> headers;
    public final InputStream data;

    public WebResourceResponse(String mimeType, String encoding, int statusCode,
            String reasonPhrase, Map<String, String> headers, InputStream data) {
        this.mimeType = mimeType;
        this.encoding = encoding;
        this.statusCode = statusCode;
        this.reasonPhrase = reasonPhrase;
        this.headers = headers;
        this.data = data;
    }
}
