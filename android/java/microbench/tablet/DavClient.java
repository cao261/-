package microbench.tablet;

import java.io.ByteArrayInputStream;
import java.nio.charset.StandardCharsets;
import java.io.BufferedInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.text.SimpleDateFormat;
import java.util.Locale;
import java.util.TimeZone;
import javax.net.ssl.SSLParameters;
import javax.net.ssl.SSLSocket;
import javax.net.ssl.SSLSocketFactory;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.URL;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import javax.xml.parsers.DocumentBuilder;
import javax.xml.parsers.DocumentBuilderFactory;

import org.w3c.dom.Element;
import org.w3c.dom.Node;
import org.w3c.dom.NodeList;

/**
 * 坚果云 WebDAV 客户端。
 *
 * 坚果云给每个账号提供一组「应用密码」，配合账号邮箱就能走 WebDAV：
 *   地址   https://dav.jianguoyun.com/dav/
 *   用户名 登录邮箱
 *   密码   应用密码（不是账号密码！网页版设置里生成）
 *
 * 用到的动词：
 *   PROPFIND 列目录（Depth: 1）
 *   GET      下载
 *   PUT      新建/覆盖
 *   MKCOL    建目录
 *   DELETE   删除
 */
public class DavClient {

    public static final String DEFAULT_ROOT = "https://dav.jianguoyun.com/dav/";

    /** 一次请求的上限：PDF 也就几 MB，够用。 */
    private static final int TIMEOUT_MS = 60_000;

    /**
     * 单次下载的体积上限。原文 PDF 几 MB 到几十 MB, 128 MB 足够 ——
     * 同时也防住畸形响应（错误的 Content-Length / chunk-size）把内存吃光。
     */
    private static final long MAX_DOWNLOAD_BYTES = 128L * 1024 * 1024;

    // ------------------------------------------------------------------
    // 限流
    //
    // 坚果云对请求频率很敏感，超了会返回：
    //   HTTP 503
    //   <s:exception>BlockedTemporarily</s:exception>
    //   <s:message>Too many requests are received recently</s:message>
    //
    // 而一次完整同步是**几十个**请求：列目录每个目录一次 PROPFIND，
    // 拉正文每篇笔记一次 GET。30 篇笔记 + 25 个目录 = 50+ 个请求 ——
    // 不节流必然被限流，用户看到的���是「刚才还好好的，这会儿又失败了」。
    // ------------------------------------------------------------------

    /** 两个请求之间的最小间隔（毫秒）。 */
    private static final long MIN_INTERVAL_MS = 200;
    /** 遇到限流时的最大重试次数。 */
    private static final int MAX_RETRY = 3;
    /** 限流时给用户的提示。 */
    private static final String RATE_LIMIT_MSG =
            "坚果云暂时限流了（请求太频繁）。等 1 分钟再试 —— "
            + "同步一整本 vault 有几十个请求，慢一点反而更快。";

    private static final Object THROTTLE_LOCK = new Object();
    private static long lastRequestAt = 0L;

    /** 全局节流：所有 WebDAV 请求共用一个闸门。 */
    private static void throttle() {
        synchronized (THROTTLE_LOCK) {
            long now = System.currentTimeMillis();
            long wait = lastRequestAt + MIN_INTERVAL_MS - now;
            if (wait > 0) {
                try {
                    Thread.sleep(wait);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
            }
            lastRequestAt = System.currentTimeMillis();
        }
    }

    /** 判断是不是坚果云的限流。 */
    private static boolean isRateLimited(int code, String body) {
        if (code != 503 || body == null) {
            return false;
        }
        return body.contains("BlockedTemporarily")
                || body.contains("Too many requests");
    }

    /** 限流后的退避等待。 */
    private static void backoff(int attempt) {
        try {
            Thread.sleep(1500L * (1L << attempt));   // 1.5s / 3s / 6s
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    private final String user;
    private final String pass;
    private String root;
    /**
     * vault 在 WebDAV 里的位置（相对 dav 根），例如 "我的坚果云/MicroBench工作台"。
     *
     * ⚠️ 这个前缀必须有 —— 网页侧给的路径是 **vault 相对路径**
     * (01_Literature/...)，而 WebDAV 的根是「我的坚果云」那一层。
     * 少了前缀，请求就会打到「我的坚果云/01_Literature/...」上 —— 那里是空的，
     * 列目录看着正常（根目录确实有东西），下载却全部 404。
     */
    private String vaultRoot;

    /** 探测 vault 位置时的 PROPFIND 预算（每发一次都是一次 TLS 握手）。 */
    private static final int MAX_PROPFIND_CALLS = 12;
    private int probeCalls = 0;

    public DavClient(String user, String pass) {
        this(user, pass, DEFAULT_ROOT);
    }

    public DavClient(String user, String pass, String root) {
        this(user, pass, root, "");
    }

    public DavClient(String user, String pass, String root, String vaultRoot) {
        this.user = user == null ? "" : user.trim();
        this.pass = pass == null ? "" : pass.trim();
        String r = (root == null || root.trim().isEmpty() ? DEFAULT_ROOT : root.trim());
        // ⚠ 只允许 https。清单里的 usesCleartextTraffic=false 拦得住
        //   HttpURLConnection, **拦不住裸 socket**（PROPFIND/MKCOL 走的那条）——
        //   那样 Basic Auth 的应用密码就是明文过网。
        //   唯一的例外是回环地址：测试用的假 WebDAV 服务器就跑在本机，
        //   回环上的流量根本不离开这台机器。
        if (r.toLowerCase().startsWith("http://") && !isLoopback(r)) {
            r = "https://" + r.substring("http://".length());
        }
        this.root = r.endsWith("/") ? r : r + "/";
        this.vaultRoot = normalizeVaultRoot(vaultRoot);
    }

    /** 根是不是回环地址（测试服务器 / 本机服务）。 */
    public static boolean isLoopback(String url) {
        try {
            String h = new URL(url).getHost();
            if (h == null) {
                return false;
            }
            h = h.toLowerCase();
            return "localhost".equals(h) || "::1".equals(h) || "[::1]".equals(h)
                    || "127.0.0.1".equals(h) || h.startsWith("127.");
        } catch (Exception e) {
            return false;
        }
    }

    public String vaultRoot() {
        return vaultRoot;
    }

    private static String normalizeVaultRoot(String p) {
        if (p == null) {
            return "";
        }
        String s = p.trim().replace('\\', '/');
        while (s.startsWith("/")) {
            s = s.substring(1);
        }
        while (s.endsWith("/")) {
            s = s.substring(0, s.length() - 1);
        }
        return s;
    }

    public void setVaultRoot(String p) {
        this.vaultRoot = normalizeVaultRoot(p);
    }

    public boolean isConfigured() {
        return !user.isEmpty() && !pass.isEmpty();
    }

    /** 把 vault 相对路径拼成 WebDAV URL（逐段编码，中文与空格都要编码）。 */
    public String urlFor(String path) {
        StringBuilder sb = new StringBuilder(root);
        // 先拼 vault 前缀
        if (!vaultRoot.isEmpty()) {
            for (String seg : vaultRoot.split("/")) {
                if (!seg.isEmpty()) {
                    sb.append(encodeSegment(seg)).append('/');
                }
            }
        }
        return appendPath(sb, path);
    }

    /** 同上，但**不带** vault 前缀 —— 探测 vault 位置时要用。 */
    public String rawUrlFor(String path) {
        return appendPath(new StringBuilder(root), path);
    }

    private static String appendPath(StringBuilder sb, String path) {
        // 记住根的结束位置 —— 去尾斜杠时不能把根自己的 "/" 也删掉
        // ("https://host/dav/" 变成 ".../dav" 的话, WebDAV 根目录就打偏了)
        int rootLen = sb.length();
        String p = path == null ? "" : path.replace('\\', '/');
        for (String seg : p.split("/")) {
            if (seg.isEmpty() || ".".equals(seg) || "..".equals(seg)) {
                continue;
            }
            sb.append(encodeSegment(seg)).append('/');
        }
        while (sb.length() > rootLen && sb.charAt(sb.length() - 1) == '/') {
            sb.setLength(sb.length() - 1);
        }
        return sb.toString();
    }

    private static String encodeSegment(String seg) {
        try {
            // UTF-8 百分号编码，但不把 / 编进去（已经按段拆过了）
            StringBuilder out = new StringBuilder();
            byte[] raw = seg.getBytes("UTF-8");
            for (byte b : raw) {
                int c = b & 0xFF;
                boolean safe = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z')
                        || (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.'
                        || c == '~';
                if (safe) {
                    out.append((char) c);
                } else {
                    out.append('%').append(String.format("%02X", c));
                }
            }
            return out.toString();
        } catch (Exception e) {
            return seg;
        }
    }

    private HttpURLConnection open(String method, String path, String depth)
            throws IOException {
        throttle();
        URL u = new URL(urlFor(path));
        HttpURLConnection c = (HttpURLConnection) u.openConnection();
        c.setRequestMethod(method);
        c.setConnectTimeout(TIMEOUT_MS);
        c.setReadTimeout(TIMEOUT_MS);
        c.setRequestProperty("Authorization", basicAuth());
        c.setRequestProperty("User-Agent", "MicroBench-Tablet/1.0");
        if (depth != null) {
            c.setRequestProperty("Depth", depth);
        }
        return c;
    }

    private String basicAuth() {
        return "Basic " + base64(user + ":" + pass);
    }

    /**
     * Base64 编码。
     *
     * ⚠ 为什么自己写, 不用现成的:
     *   - `java.util.Base64` 是 **API 26** 才有的类, 而本应用 minSdk 是 24。
     *     d8 不会拦它（只在 --min-api 之下给个警告）, 编译能过 ——
     *     但在 Android 7.0/7.1 上第一次网络请求就 NoClassDefFoundError,
     *     整个应用直接不可用。
     *   - `android.util.Base64` 从 API 8 就有, 可惜是 Android 专有的,
     *     桌面 JVM 上跑不了（我们用假 WebDAV 服务器对拍测试）。
     * 自己写 20 行, 两边都能跑, 而且不绑任何 API 级别。
     */
    public static String base64(String raw) {
        byte[] data;
        try {
            data = raw.getBytes("UTF-8");
        } catch (Exception e) {
            data = raw.getBytes();
        }
        final String T =
                "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
        StringBuilder sb = new StringBuilder(((data.length + 2) / 3) * 4);
        int i = 0;
        while (i + 2 < data.length) {
            int v = ((data[i] & 0xFF) << 16)
                    | ((data[i + 1] & 0xFF) << 8)
                    | (data[i + 2] & 0xFF);
            sb.append(T.charAt((v >> 18) & 63));
            sb.append(T.charAt((v >> 12) & 63));
            sb.append(T.charAt((v >> 6) & 63));
            sb.append(T.charAt(v & 63));
            i += 3;
        }
        int rest = data.length - i;
        if (rest == 1) {
            int v = (data[i] & 0xFF) << 16;
            sb.append(T.charAt((v >> 18) & 63));
            sb.append(T.charAt((v >> 12) & 63));
            sb.append('=').append('=');
        } else if (rest == 2) {
            int v = ((data[i] & 0xFF) << 16) | ((data[i + 1] & 0xFF) << 8);
            sb.append(T.charAt((v >> 18) & 63));
            sb.append(T.charAt((v >> 12) & 63));
            sb.append(T.charAt((v >> 6) & 63));
            sb.append('=');
        }
        return sb.toString();
    }

    // ------------------------------------------------------------------
    // 底层 HTTP
    // ------------------------------------------------------------------
    //
    // ⚠️ HttpURLConnection.setRequestMethod() **只放行标准方法**
    // (GET/POST/PUT/DELETE/HEAD/OPTIONS/TRACE)，传 "PROPFIND" 会抛
    // ProtocolException: Invalid HTTP method。WebDAV 的目录列举全靠
    // PROPFIND，MKCOL 也一样。
    //
    // 两种解法:
    //   a) 反射改 OkHttp 内部的 Request.Builder.method —— Android 9+ 的
    //      隐藏 API 限制会让它变得脆弱
    //   b) 自己发 HTTP —— 完全可控，跨版本稳定
    // 选 b。但只让 PROPFIND / MKCOL 走自己写的 socket; GET / PUT / DELETE
    // 仍然走成熟的 HttpURLConnection（断点续传、连接池、错误处理都更可靠）。

    /** 一条 HTTP 响应。 */
    private static class Resp {
        int code;
        Map<String, List<String>> headers = new LinkedHashMap<>();
        byte[] body = new byte[0];

        String header(String name) {
            List<String> v = headers.get(name.toLowerCase());
            return (v == null || v.isEmpty()) ? null : v.get(0);
        }
    }

    /** 用裸 socket 发一个请求, 支持任意方法。 */
    private Resp rawRequest(String method, String path, byte[] body,
                             String contentType, String depth) throws IOException {
        return rawRequest(method, path, body, contentType, depth, true);
    }

    /**
     * @param vaultRelative true=path 相对 vault 根（会带上 vaultRoot 前缀）
     *                      false=path 相对 WebDAV 根（探测 vault 位置时用）
     */
    private Resp rawRequest(String method, String path, byte[] body,
                             String contentType, String depth,
                             boolean vaultRelative) throws IOException {
        throttle();
        URL u = new URL(vaultRelative ? urlFor(path) : rawUrlFor(path));
        String proto = u.getProtocol();
        int port = u.getPort() > 0 ? u.getPort() : ("https".equals(proto) ? 443 : 80);
        String host = u.getHost();
        String target = u.getFile();
        if (target == null || target.isEmpty()) {
            target = "/";
        }

        Socket sock;
        if ("https".equals(proto)) {
            // ⚠️ 必须用**无参** createSocket()。
            //
            // 无参版本返回的是一个**还没连接、还没握手**的 socket —— 握手
            // 发生在后面的 connect() 里。所以顺序必须是:
            //     createSocket() -> setSSLParameters() -> connect()
            // 这样 endpointIdentificationAlgorithm=HTTPS 才真正生效,
            // TLS 会校验证书上的主机名是不是 dav.jianguoyun.com。
            //
            // 反过来用 createSocket(host, port) 是**错**的: 那个版本在
            // createSocket 内部就把 TLS 握手做完了, 这时再 setSSLParameters
            // 已经来不及 —— 主机名验证等于没开, 坚果云的应用密码就
            // 裸奔在中间人面前。
            SSLSocket ss;
            try {
                ss = (SSLSocket) SSLSocketFactory.getDefault().createSocket();
            } catch (Exception e) {
                throw new IOException("无法创建 TLS 连接: " + e.getMessage());
            }
            try {
                SSLParameters params = ss.getSSLParameters();
                params.setEndpointIdentificationAlgorithm("HTTPS");
                ss.setSSLParameters(params);
            } catch (Exception ignored) {
                // 老实现不支持这个开关 —— 不该崩, 只是少了主机名验证
            }
            sock = ss;
        } else {
            sock = new Socket();
        }
        try {
            sock.connect(new InetSocketAddress(host, port), TIMEOUT_MS);
            sock.setSoTimeout(TIMEOUT_MS);

            // 双保险：握手完再验一次主机名。
            // 万一上面的 setEndpointIdentificationAlgorithm 被老实现吞掉了
            // （我们 catch 了那个异常），这里还能拦下来 ——
            // 坚果云的应用密码是长期凭证，裸奔一次就全泄露了。
            if (sock instanceof SSLSocket) {
                try {
                    if (!javax.net.ssl.HttpsURLConnection.getDefaultHostnameVerifier()
                            .verify(host, ((SSLSocket) sock).getSession())) {
                        throw new IOException("服务器证书的主机名对不上: " + host);
                    }
                } catch (javax.net.ssl.SSLPeerUnverifiedException e) {
                    throw new IOException("服务器证书的主机名对不上: " + host);
                } catch (IOException e) {
                    throw e;
                } catch (Exception e) {
                    // 主机名验证器本身不可用 —— 不因为这个把连接掐断,
                    // 上面那层参数已经生效了。
                }
            }

            StringBuilder head = new StringBuilder();
            head.append(method).append(' ').append(target).append(" HTTP/1.1\r\n");
            head.append("Host: ").append(host);
            if (!((proto.equals("http") && port == 80)
                    || (proto.equals("https") && port == 443))) {
                head.append(':').append(port);
            }
            head.append("\r\n");
            head.append("Authorization: ").append(basicAuth()).append("\r\n");
            head.append("User-Agent: MicroBench-Tablet/1.0\r\n");
            head.append("Connection: close\r\n");
            if (depth != null) {
                head.append("Depth: ").append(depth).append("\r\n");
            }
            if (contentType != null) {
                head.append("Content-Type: ").append(contentType).append("\r\n");
            }
            if (body != null) {
                head.append("Content-Length: ").append(body.length).append("\r\n");
            }
            head.append("\r\n");

            OutputStream os = sock.getOutputStream();
            os.write(head.toString().getBytes(StandardCharsets.ISO_8859_1));
            if (body != null) {
                os.write(body);
            }
            os.flush();

            return readResponse(sock.getInputStream());
        } finally {
            try {
                sock.close();
            } catch (IOException ignored) {
            }
        }
    }

    private static Resp readResponse(InputStream in) throws IOException {
        // 裸 socket 逐字节读很慢 —— 套一层缓冲
        BufferedInputStream bin = new BufferedInputStream(in, 32 * 1024);
        Resp r = new Resp();
        String status = readLine(bin);
        if (status == null) {
            throw new IOException("服务器没返回状态行");
        }
        String[] parts = status.split(" ", 3);
        try {
            r.code = Integer.parseInt(parts[1]);
        } catch (Exception e) {
            throw new IOException("看不懂的状态行: " + status);
        }

        String line;
        while ((line = readLine(bin)) != null && !line.isEmpty()) {
            int c = line.indexOf(':');
            if (c <= 0) {
                continue;
            }
            String k = line.substring(0, c).trim().toLowerCase();
            String v = line.substring(c + 1).trim();
            List<String> list = r.headers.get(k);
            if (list == null) {
                list = new ArrayList<>();
                r.headers.put(k, list);
            }
            list.add(v);
        }

        String te = r.header("transfer-encoding");
        if (te != null && te.toLowerCase().contains("chunked")) {
            r.body = readChunked(bin);
        } else {
            String cl = r.header("content-length");
            long n = 0;
            if (cl != null) {
                try {
                    n = Long.parseLong(cl.trim());
                } catch (NumberFormatException ignored) {
                }
            }
            if (n > MAX_DOWNLOAD_BYTES) {
                throw new IOException("响应体超过上限 "
                        + (MAX_DOWNLOAD_BYTES / 1048576) + " MB");
            }
            if (n > 0) {
                r.body = readFully(bin, (int) n);
            } else {
                // ⚠ 没有 Content-Length、也不是 chunked。
                //   有些服务器（尤其是走反代的）用「关连接」表示结束。
                //   这时必须**读到 EOF** —— 否则 body 被当成空的,
                //   PROPFIND 解析出 0 条目, 页面上显示「目录为空」,
                //   真空目录和解析失败根本分不开。
                r.body = readAllBytes(bin, MAX_DOWNLOAD_BYTES, 16 * 1024);
            }
        }
        return r;
    }

    private static String readLine(InputStream in) throws IOException {
        java.io.ByteArrayOutputStream bo = new java.io.ByteArrayOutputStream(128);
        int c;
        boolean any = false;
        while ((c = in.read()) != -1) {
            any = true;
            if (c == '\n') {
                break;
            }
            if (c != '\r') {
                bo.write(c);
            }
        }
        if (!any && c == -1) {
            return null;
        }
        return new String(bo.toByteArray(), StandardCharsets.UTF_8);
    }

    private static byte[] readChunked(InputStream in) throws IOException {
        java.io.ByteArrayOutputStream out = new java.io.ByteArrayOutputStream(16 * 1024);
        long total = 0;
        while (true) {
            String sizeLine = readLine(in);
            if (sizeLine == null) {
                break;
            }
            int semi = sizeLine.indexOf(';');
            if (semi >= 0) {
                sizeLine = sizeLine.substring(0, semi);
            }
            int size;
            try {
                size = Integer.parseInt(sizeLine.trim(), 16);
            } catch (NumberFormatException e) {
                // 畸形 chunk-size —— 明确报错, **别静默 break**。
                // 静默截断的话上层只看到一句「DAV error」, 完全查不出原因;
                // 而且 size 直接来自网络, 7FFFFFFF 会让 new byte[n] 抛
                // OutOfMemoryError —— 那是 Error, catch (Exception) 抓不住,
                // 线程直接死。
                throw new IOException("服务器返回了畸形的 chunk-size: " + sizeLine.trim());
            }
            if (size < 0) {
                throw new IOException("服务器返回了负数的 chunk-size: " + size);
            }
            if (size == 0) {
                // 读完 trailer
                String t;
                while ((t = readLine(in)) != null && !t.isEmpty()) {
                    // 忽略
                }
                break;
            }
            total += size;
            if (total > MAX_DOWNLOAD_BYTES) {
                throw new IOException("响应体超过上限 "
                        + (MAX_DOWNLOAD_BYTES / 1048576) + " MB");
            }
            out.write(readFully(in, size));
            readLine(in);   // 每个 chunk 后面的 CRLF
        }
        return out.toByteArray();
    }

    private static byte[] readFully(InputStream in, int n) throws IOException {
        byte[] buf = new byte[n];
        int off = 0;
        while (off < n) {
            int r = in.read(buf, off, n - off);
            if (r < 0) {
                break;
            }
            off += r;
        }
        if (off == n) {
            return buf;
        }
        byte[] out = new byte[off];
        System.arraycopy(buf, 0, out, 0, off);
        return out;
    }

    /** 下载一个文件。返回 null 表示不存在（404）。 */
    public byte[] get(String path) throws IOException {
        // ⚠ 限流退避（跟 list 一样）
        for (int attempt = 0; attempt <= MAX_RETRY; attempt++) {
            HttpURLConnection c = open("GET", path, null);
            int code = c.getResponseCode();
            if (code == 404) {
                c.disconnect();
                return null;
            }
            if (code == 503) {
                String err = readAll(c.getErrorStream());
                c.disconnect();
                if (isRateLimited(code, err)) {
                    if (attempt == MAX_RETRY) {
                        throw new IOException(RATE_LIMIT_MSG);
                    }
                    backoff(attempt);
                    continue;
                }
                throw new IOException("WebDAV GET " + path + " -> HTTP " + code
                        + (err.isEmpty() ? "" : " " + err));
            }
            if (code != 200) {
                String err = readAll(c.getErrorStream());
                c.disconnect();
                throw new IOException("WebDAV GET " + path + " -> HTTP " + code
                        + (err.isEmpty() ? "" : " " + err));
            }
            // Content-Length 预检：太大的直接拒掉, 别把内存吃光。
            // 原文 PDF 最大的 9.3 MB, 128 MB 的上限绰绰有余。
            String cl = c.getHeaderField("Content-Length");
            long expect = 0;
            if (cl != null) {
                try {
                    expect = Long.parseLong(cl.trim());
                } catch (NumberFormatException ignored) {
                }
            }
            if (expect > MAX_DOWNLOAD_BYTES) {
                c.disconnect();
                throw new IOException("文件太大（" + (expect / 1048576) + " MB），超过上限");
            }
            try (InputStream in = new BufferedInputStream(c.getInputStream(), 32 * 1024)) {
                return readAllBytes(in, MAX_DOWNLOAD_BYTES, (int) Math.min(expect, 8L * 1024 * 1024));
            } finally {
                c.disconnect();
            }
        }
        throw new IOException(RATE_LIMIT_MSG);
    }

    /**
     * 只读文件开头的一小段（默认 64 KB），用来验证**下载链路**通不通。
     *
     * 之前是整篇 PDF 读进内存只为了看一眼 —— 一篇 9 MB 的论文,
     * 「测试连接」这一下就能把平板的内存吃掉一大块，还把等待时间拉长。
     */
    public int getPartial(String path, int maxBytes) throws IOException {
        HttpURLConnection c = open("GET", path, null);
        int code = c.getResponseCode();
        try {
            if (code != 200) {
                String err = readAll(c.getErrorStream());
                throw new IOException("WebDAV GET " + path + " -> HTTP " + code
                        + (err.isEmpty() ? "" : " " + err));
            }
            try (InputStream in = new BufferedInputStream(c.getInputStream(), 16 * 1024)) {
                byte[] buf = new byte[Math.max(1, maxBytes)];
                int off = 0;
                while (off < buf.length) {
                    int r = in.read(buf, off, buf.length - off);
                    if (r < 0) {
                        break;
                    }
                    off += r;
                }
                return off;
            }
        } finally {
            c.disconnect();
        }
    }

    /** 列目录。返回 null 表示不是目录或不存在。 */
    public List<DavEntry> list(String dir) throws Exception {
        String body = "<?xml version=\"1.0\" encoding=\"utf-8\"?>"
                + "<D:propfind xmlns:D=\"DAV:\"><D:prop>"
                + "<D:resourcetype/><D:getcontentlength/><D:getlastmodified/>"
                + "</D:prop></D:propfind>";
        // PROPFIND 不是标准方法, HttpURLConnection 不认 -> 走裸 socket
        // ⚠ 限流退避：坚果云返回 503 BlockedTemporarily 时等一下再试，
        //   而不是直接把 503 甩给用户（用户会以为「配置错了」）。
        Resp r = null;
        for (int attempt = 0; attempt <= MAX_RETRY; attempt++) {
            r = rawRequest("PROPFIND", dir, body.getBytes(StandardCharsets.UTF_8),
                    "application/xml; charset=utf-8", "1");
            String text = new String(r.body, StandardCharsets.UTF_8);
            if (isRateLimited(r.code, text)) {
                if (attempt == MAX_RETRY) {
                    throw new IOException(RATE_LIMIT_MSG);
                }
                backoff(attempt);
                continue;
            }
            if (r.code != 207 && r.code != 200) {
                throw new IOException("WebDAV PROPFIND " + dir + " -> HTTP " + r.code
                        + (r.body.length > 0 ? " " + text : ""));
            }
            break;
        }
        return parseMultiStatus(new String(r.body, StandardCharsets.UTF_8), dir);
    }

    private List<DavEntry> parseMultiStatus(String xml, String baseDir) throws Exception {
        List<DavEntry> out = new ArrayList<>();
        DocumentBuilderFactory f = DocumentBuilderFactory.newInstance();
        // WebDAV 响应来自我们自己配置的服务器，但仍然关掉外部实体
        f.setNamespaceAware(true);
        try {
            f.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true);
            f.setFeature("http://xml.org/sax/features/external-general-entities", false);
            f.setFeature("http://xml.org/sax/features/external-parameter-entities", false);
        } catch (Exception ignored) {
            // 老解析器不支持这些开关，忽略
        }
        DocumentBuilder b = f.newDocumentBuilder();
        org.w3c.dom.Document doc = b.parse(new ByteArrayInputStream(xml.getBytes(StandardCharsets.UTF_8)));
        NodeList responses = doc.getElementsByTagNameNS("DAV:", "response");
        for (int i = 0; i < responses.getLength(); i++) {
            Element resp = (Element) responses.item(i);
            String href = textOf(resp, "DAV:", "href");
            boolean isDir = hasCollection(resp);
            long size = longOf(resp, "DAV:", "getcontentlength");
            if (href == null || href.isEmpty()) {
                continue;
            }
            // href 是 URL 路径，先解码，再去掉 dav 根前缀
            String decoded = urlDecode(href);
            String rel = relativize(decoded);
            if (rel == null || rel.isEmpty()) {
                continue;   // 就是目录自己本身
            }
            if (".".equals(rel) || "..".equals(rel)) {
                continue;
            }
            DavEntry e = new DavEntry();
            e.path = rel;
            e.name = rel.substring(rel.lastIndexOf('/') + 1);
            e.isDir = isDir;
            e.size = size;
            e.mtime = parseHttpDate(textOf(resp, "DAV:", "getlastmodified"));
            out.add(e);
        }
        return out;
    }

    private static boolean hasCollection(Element resp) {
        NodeList list = resp.getElementsByTagNameNS("DAV:", "collection");
        return list.getLength() > 0;
    }

    private static String textOf(Element parent, String ns, String local) {
        NodeList list = parent.getElementsByTagNameNS(ns, local);
        if (list.getLength() == 0) {
            return null;
        }
        return list.item(0).getTextContent();
    }

    private static long longOf(Element parent, String ns, String local) {
        String t = textOf(parent, ns, local);
        if (t == null) {
            return 0;
        }
        try {
            return Long.parseLong(t.trim());
        } catch (NumberFormatException e) {
            return 0;
        }
    }

    /**
     * 解析 WebDAV 的 getlastmodified（RFC 1123，例如
     * "Tue, 25 Oct 2026 03:14:15 GMT"）→ epoch 毫秒。
     *
     * ⚠ 不用 java.time（API 26 才有），用 SimpleDateFormat（API 1）。
     */
    private static long parseHttpDate(String s) {
        if (s == null) {
            return 0;
        }
        try {
            SimpleDateFormat f =
                    new SimpleDateFormat("EEE, dd MMM yyyy HH:mm:ss zzz", Locale.US);
            f.setTimeZone(TimeZone.getTimeZone("GMT"));
            return f.parse(s.trim()).getTime();
        } catch (Exception e) {
            return 0;
        }
    }

    /** 把 WebDAV 返回的绝对 href 变成 vault 相对路径。 */
    private String relativize(String href) {
        String h = href;
        int q = h.indexOf('?');
        if (q >= 0) {
            h = h.substring(0, q);
        }
        // 去掉 scheme://host
        int slash = h.indexOf('/', h.indexOf("//") + 2);
        if (slash >= 0) {
            h = h.substring(slash + 1);
        }
        // 去掉 dav 根（如 "dav/我的坚果云/xxx"）
        String marker = "dav/";
        int m = h.indexOf(marker);
        if (m >= 0) {
            h = h.substring(m + marker.length());
        }
        // 再去掉 vault 前缀（网页侧用的是 vault 相对路径）
        if (!vaultRoot.isEmpty()) {
            String vr = vaultRoot + "/";
            if (h.startsWith(vr)) {
                h = h.substring(vr.length());
            } else if (h.equals(vaultRoot)) {
                h = "";
            }
        }
        h = h.replaceFirst("^/+", "").replaceFirst("/+$", "");
        return h;
    }

    public static String urlDecode(String s) {
        try {
            // ⚠ URLDecoder 走的是 **form 编码**规则 —— 它会把 "+" 解成空格。
            //   但路径里的 "+" 是字面量：「C++入门.pdf」会被解成「C 入门.pdf」，
            //   列目录看着正常，点开却永远 404。
            //   先把 "+" 换成 %2B 再解，就对了。
            return java.net.URLDecoder.decode(s.replace("+", "%2B"), "UTF-8");
        } catch (Exception e) {
            return s;
        }
    }

    /**
     * 自动找到**同步根**在 WebDAV 里的位置。
     *
     * 同步根是 vault 根的**兄弟目录**：
     *   vault 根  「我的坚果云/MicroBench工作台」   —— 笔记与原文 PDF
     *   同步根    「我的坚果云/MicroBench同步」     —— 译文快照 + 批注（两台设备共享）
     *
     * ⚠ 没有同步根，平板就只剩「能看 PDF」——
     *   跟用微信直接打开一个 PDF 没区别。译文才是它跟电脑端对等的地方。
     *
     * 判据：兄弟目录存在，且里面能看到 snapshots/ 或 annotations/。
     */
    public String detectSyncRoot(String vaultRoot) {
        String vr = normalizeVaultRoot(vaultRoot);
        if (vr.isEmpty()) {
            return "";
        }
        int i = vr.lastIndexOf('/');
        if (i <= 0) {
            return "";
        }
        String parent = vr.substring(0, i);
        String candidate = parent + "/MicroBench同步";
        try {
            DavClient probe = new DavClient(user, pass, root, candidate);
            List<DavEntry> entries = probe.list("");
            for (DavEntry e : entries) {
                if (e.isDir && isSyncMarkerDir(e.name)) {
                    return candidate;
                }
            }
            // 目录在但还是空的 —— 也认（先存着，等 PC 那边产生内容）
            if (entries.isEmpty()) {
                return candidate;
            }
        } catch (Exception e) {
            // 继续走兄弟目录扫描
        }
        // ⚠ 兜底：同步根不一定叫「MicroBench同步」（用户可能改过名）。
        //   在 vault 的**兄弟目录**里找那个含 snapshots/annotations 的。
        try {
            DavClient probe = new DavClient(user, pass, root, parent);
            for (DavEntry e : probe.list("")) {
                if (!e.isDir) {
                    continue;
                }
                try {
                    DavClient sub = new DavClient(user, pass, root, e.path);
                    for (DavEntry s : sub.list("")) {
                        if (s.isDir && isSyncMarkerDir(s.name)) {
                            return e.path;
                        }
                    }
                } catch (Exception ex) {
                    // 跳过这个兄弟目录
                }
            }
        } catch (Exception e) {
            return "";
        }
        return "";
    }

    /**
     * 自动找到 vault 在云盘里的位置。
     *
     * 判据: 哪个目录下面有 `01_Literature` / `00_Dashboard`，
     * 或者直接躺着 .md 笔记。
     * 典型路径是「我的坚果云/MicroBench工作台」—— 在 WebDAV 根下面两层,
     * 所以必须**递归**找, 只看一两层会漏掉。
     */
    public String detectVaultRoot() {
        probeCalls = 0;
        return detectVaultAt("", 3);
    }

    /**
     * 递归找 vault 位置。
     *
     * ⚠⚠ **名字不能用来区分 vault 和同步根**（2026-10-25 用户真机故障）：
     *   真实云盘长这样 —— 两个**兄弟**目录，名字共享前缀：
     *       我的坚果云/
     *         ├── MicroBench工作台/   ← vault（PDF 原文、笔记）
     *         └── MicroBench同步/     ← 同步根（译文快照、批注）
     *   老实现只按名字判断（"含 microbench 就算"），
     *   而坚果云按名字排序，「同」(U+540C) 排在「工」(U+5DE5) **前面**
     *   → 「MicroBench同步」**永远**先被选中 → 自愈把**错的位置**存了下来。
     *   → 症状：界面显示"已连接"、vault 位置亮出「我的坚果云/MicroBench同步」，
     *     但列目录 0 条、PDF 全 404（同步根里当然没有 01_Literature）。
     *
     *   **正确判据是内容**：
     *     - vault 里有 00_Dashboard / 01_Literature / …；
     *     - 同步根里有 snapshots / annotations。
     *   所以：名字只用来"值得一看"，**必须验证内容**才能认。
     */
    private String detectVaultAt(String dir, int depth) {
        // 探测是要发 PROPFIND 的, 每一次都是一次完整的 TLS 握手。
        // 云盘里目录一多, 「测试并保存」能卡好几分钟 —— 给它一个预算,
        // 超了就让用户手动填 vault 位置, 别把人吊在那儿。
        if (probeCalls >= MAX_PROPFIND_CALLS) {
            return null;
        }
        probeCalls++;
        List<DavEntry> entries;
        try {
            entries = listRaw(dir);
        } catch (Exception e) {
            return null;
        }
        boolean hasLit = false;
        boolean hasMd = false;
        boolean isSyncRoot = false;
        List<DavEntry> dirs = new ArrayList<>();
        for (DavEntry e : entries) {
            if (e.isDir) {
                dirs.add(e);
                if (isVaultMarkerDir(e.name)) {
                    hasLit = true;
                } else if (isSyncMarkerDir(e.name)) {
                    isSyncRoot = true;
                }
            } else if (e.name.toLowerCase().endsWith(".md")) {
                hasMd = true;
            }
        }
        if (hasLit) {
            return dir;                     // 最强信号：这一层就有 vault 的标志目录
        }
        if (isSyncRoot) {
            // 这一层是**同步根** → 绝对不是 vault。
            // 必须在往下钻之前拦住：同步根里 snapshots/ 下全是 .md，
            // 不拦的话会返回「…/MicroBench同步/snapshots」这种更离谱的路径。
            return null;
        }
        if (depth <= 1) {
            return hasMd ? dir : null;
        }
        // 往下找一层, 但优先看有没有目录就叫 01_Literature
        for (DavEntry e : dirs) {
            if ("01_Literature".equals(e.name) || "00_Dashboard".equals(e.name)) {
                return e.path;
            }
        }
        // ⚠ 名字像我们的 vault → **但必须验证内容**。
        //   常见布局是
        //       我的坚果云/            <- dav 根
        //         MicroBench工作台/    <- vault
        //         MicroBench同步/      <- 同步根（名字也会命中！）
        //   命中它只要几次 PROPFIND；不命中的话，
        //   后面那个「把整朵云盘递归一遍」的兜底会发几十个请求 ——
        //   坚果云会直接回 503 BlockedTemporarily 把我们限流。
        for (DavEntry e : dirs) {
            if (!isLikelyVaultName(e.name)) {
                continue;
            }
            if (isVaultDir(e.path)) {
                return e.path;              // 验证通过：这才是 vault
            }
        }
        for (DavEntry e : dirs) {
            String found = detectVaultAt(e.path, depth - 1);
            if (found != null) {
                return found;
            }
        }
        return hasMd ? dir : null;
    }

    /**
     * vault 的标志目录 —— 只有 vault 里才有这些。
     *
     * ⚠ 不能用「有没有 .md」当判据：同步根的 snapshots/ 里也全是 .md。
     */
    private static boolean isVaultMarkerDir(String name) {
        if (name == null) {
            return false;
        }
        return "00_Dashboard".equals(name) || "01_Literature".equals(name)
                || "02_Reproduction".equals(name) || "03_Knowledge".equals(name)
                || "04_Weekly_Reports".equals(name) || "_Templates".equals(name);
    }

    /** 同步根的标志目录 —— 只有同步根里才有这些。 */
    private static boolean isSyncMarkerDir(String name) {
        if (name == null) {
            return false;
        }
        return "snapshots".equals(name) || "annotations".equals(name);
    }

    /**
     * 验证一个目录**确实是 vault**（里面有没有 vault 的标志目录）。
     *
     * 名字会撞车（vault 和同步根都叫 MicroBenchxxx）→ 只能靠内容分辨。
     */
    private boolean isVaultDir(String path) {
        if (probeCalls >= MAX_PROPFIND_CALLS) {
            return false;
        }
        probeCalls++;
        try {
            for (DavEntry e : listRaw(path)) {
                if (e.isDir && isVaultMarkerDir(e.name)) {
                    return true;
                }
            }
        } catch (Exception e) {
            return false;
        }
        return false;
    }

    /** 目录名像不像我们的 vault（省得在别人的文件夹里瞎逛）。 */
    private static boolean isLikelyVaultName(String name) {
        if (name == null) {
            return false;
        }
        String n = name.toLowerCase();
        return n.contains("microbench") || n.contains("工作台");
    }

    /** 列目录（不带 vault 前缀的原始 WebDAV 路径）—— 探测 vault 位置时用。 */
    private List<DavEntry> listRaw(String dir) throws Exception {
        String body = "<?xml version=\"1.0\" encoding=\"utf-8\"?>"
                + "<D:propfind xmlns:D=\"DAV:\"><D:prop>"
                + "<D:resourcetype/><D:getcontentlength/><D:getlastmodified/>"
                + "</D:prop></D:propfind>";
        Resp r = rawRequest("PROPFIND", dir, body.getBytes(StandardCharsets.UTF_8),
                "application/xml; charset=utf-8", "1", false);
        if (r.code != 207 && r.code != 200) {
            throw new IOException("WebDAV PROPFIND " + dir + " -> HTTP " + r.code);
        }
        return parseMultiStatus(new String(r.body, StandardCharsets.UTF_8), dir);
    }

    /**
     * 找一个**小文件**来验证下载链路。
     *
     * ⚠ 原来找的是 PDF —— 但 PDF 在 vault 里深达 3 层，为找它要发好几个
     *   PROPFIND，最后还要 GET 9 MB。验「下载链路通不通」只需要一个小文件：
     *   几 KB 的 .md 就够（同一个 GET 通道），代价小两个数量级，
     *   也不会那么容易把坚果云打到限流。
     */
    public String findSmallestNote(String dir, int maxDepth) {
        List<DavEntry> entries;
        try {
            entries = list(dir);
        } catch (Exception e) {
            return null;
        }
        String best = null;
        long bestSize = Long.MAX_VALUE;
        for (DavEntry e : entries) {
            if (!e.isDir && e.name.toLowerCase().endsWith(".md") && e.size < bestSize) {
                bestSize = e.size;
                best = e.path;
            }
        }
        if (best != null) {
            return best;
        }
        if (maxDepth <= 1) {
            return null;
        }
        for (DavEntry e : entries) {
            if (e.isDir) {
                String found = findSmallestNote(e.path, maxDepth - 1);
                if (found != null) {
                    return found;
                }
            }
        }
        return null;
    }

    /**
     * 递归找一个 PDF（最多 maxDepth 层），用来做「下载链路」自检。
     * 根目录下面通常只有目录, PDF 在 MicroBench工作台/01_Literature/... 里。
     */
    public String findFirstPdf(String dir, int maxDepth) {
        return findFirstPdf(dir, maxDepth, new int[]{0});
    }

    private String findFirstPdf(String dir, int maxDepth, int[] budget) {
        // 同样有预算 —— 否则在云盘里能逛到天荒地老，
        // 而且每个目录一次 PROPFIND，会把坚果云打到限流。
        if (budget[0]++ >= 6) {
            return null;
        }
        List<DavEntry> entries;
        try {
            entries = list(dir);
        } catch (Exception e) {
            return null;
        }
        for (DavEntry e : entries) {
            if (!e.isDir && e.name.toLowerCase().endsWith(".pdf")) {
                return e.path;
            }
        }
        if (maxDepth <= 1) {
            return null;
        }
        for (DavEntry e : entries) {
            if (e.isDir) {
                String found = findFirstPdf(e.path, maxDepth - 1, budget);
                if (found != null) {
                    return found;
                }
            }
        }
        return null;
    }

    // ------------------------------------------------------------------
    // 写
    // ------------------------------------------------------------------

    /** 新建或覆盖一个文件。 */
    public void put(String path, byte[] data) throws IOException {
        String parent = parentOf(path);
        if (parent != null && !parent.isEmpty()) {
            // 不用 catch 吞掉错误 —— 「目录已存在」坚果云返回 405，
            // 而 mkcol() 内部已经把 405 当成成功了。这里抛出来的
            // 一定是真错误（鉴权失败 / 网络不通），
            // 吞掉的话后面 PUT 只会报一个看不懂的 409。
            mkdirs(parent);
        }
        HttpURLConnection c = open("PUT", path, null);
        c.setDoOutput(true);
        c.setRequestProperty("Content-Type", "application/octet-stream");
        c.setFixedLengthStreamingMode(data.length);
        try (OutputStream os = c.getOutputStream()) {
            os.write(data);
        }
        int code = c.getResponseCode();
        if (code < 200 || code >= 300) {
            String err = readAll(c.getErrorStream());
            c.disconnect();
            throw new IOException("WebDAV PUT " + path + " -> HTTP " + code
                    + (err.isEmpty() ? "" : " " + err));
        }
        c.disconnect();
    }

    /** 建目录（含缺失的父目录）。 */
    public void mkdirs(String dir) throws IOException {
        String[] parts = dir.split("/");
        StringBuilder cur = new StringBuilder();
        for (String seg : parts) {
            if (seg.isEmpty()) {
                continue;
            }
            if (cur.length() > 0) {
                cur.append('/');
            }
            cur.append(seg);
            mkcol(cur.toString());
        }
    }

    private void mkcol(String dir) throws IOException {
        // MKCOL 同样不是标准方法 -> 裸 socket
        Resp r = rawRequest("MKCOL", dir, null, null, null);
        // 405 = 已存在，正是我们要的
        if (r.code != 405 && (r.code < 200 || r.code >= 300)) {
            throw new IOException("WebDAV MKCOL " + dir + " -> HTTP " + r.code);
        }
    }

    public void delete(String path) throws IOException {
        HttpURLConnection c = open("DELETE", path, null);
        int code = c.getResponseCode();
        c.disconnect();
        if (code != 404 && (code < 200 || code >= 300)) {
            throw new IOException("WebDAV DELETE " + path + " -> HTTP " + code);
        }
    }

    /** 探活：列根目录，成功就说明账号/应用密码对。 */
    public boolean probe() {
        try {
            list("").size();
            return true;
        } catch (Exception e) {
            return false;
        }
    }

    // ------------------------------------------------------------------
    // 小工具
    // ------------------------------------------------------------------

    private static String parentOf(String path) {
        int i = path.lastIndexOf('/');
        return i > 0 ? path.substring(0, i) : null;
    }

    private static String readAll(InputStream in) throws IOException {
        if (in == null) {
            return "";
        }
        try (InputStream s = in) {
            return new String(readAllBytes(s), "UTF-8");
        }
    }

    private static byte[] readAllBytes(InputStream in) throws IOException {
        return readAllBytes(in, MAX_DOWNLOAD_BYTES, 16 * 1024);
    }

    /**
     * @param limit    累计上限 —— 超过就抛 IOException（不是 OOM）
     * @param initial  预估大小，用来一次性把缓冲区开够（少几次 realloc）
     */
    private static byte[] readAllBytes(InputStream in, long limit, int initial)
            throws IOException {
        int cap = Math.max(16 * 1024, Math.min(initial, 8 * 1024 * 1024));
        java.io.ByteArrayOutputStream bo = new java.io.ByteArrayOutputStream(cap);
        byte[] buf = new byte[16 * 1024];
        int n;
        long total = 0;
        while ((n = in.read(buf)) > 0) {
            total += n;
            if (total > limit) {
                throw new IOException("响应体超过上限 " + (limit / 1048576) + " MB");
            }
            bo.write(buf, 0, n);
        }
        return bo.toByteArray();
    }

    /** 目录项。 */
    public static class DavEntry {
        public String path;
        public String name;
        public boolean isDir;
        public long size;
        /** 云端的最后修改时间（epoch 毫秒）。用来跳过没变的文件，省请求。 */
        public long mtime;

        /** 转成 JSON（字段名与网页端约定一致）。 */
        public String toJson() {
            StringBuilder sb = new StringBuilder();
            sb.append('{');
            sb.append("\"path\":").append(jsonStr(path)).append(',');
            sb.append("\"name\":").append(jsonStr(name)).append(',');
            sb.append("\"isDir\":").append(isDir).append(',');
            sb.append("\"size\":").append(size).append(',');
            sb.append("\"mtime\":").append(mtime);
            sb.append('}');
            return sb.toString();
        }

        private static String jsonStr(String s) {
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
    }
}
