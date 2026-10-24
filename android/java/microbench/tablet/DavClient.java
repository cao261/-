package microbench.tablet;

import java.io.ByteArrayInputStream;
import java.nio.charset.StandardCharsets;
import java.io.IOException;
import java.io.InputStream;
import javax.net.ssl.SSLSocketFactory;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.InetSocketAddress;
import java.net.ProtocolException;
import java.net.Socket;
import java.net.URI;
import java.net.URL;
import java.util.ArrayList;
import java.util.Base64;
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

    private final String user;
    private final String pass;
    private String root;

    public DavClient(String user, String pass) {
        this(user, pass, DEFAULT_ROOT);
    }

    public DavClient(String user, String pass, String root) {
        this.user = user == null ? "" : user.trim();
        this.pass = pass == null ? "" : pass.trim();
        String r = (root == null || root.trim().isEmpty() ? DEFAULT_ROOT : root.trim());
        this.root = r.endsWith("/") ? r : r + "/";
    }

    public boolean isConfigured() {
        return !user.isEmpty() && !pass.isEmpty();
    }

    /** 把 vault 相对路径拼成 WebDAV URL（逐段编码，中文与空格都要编码）。 */
    public String urlFor(String path) {
        StringBuilder sb = new StringBuilder(root);
        String p = path == null ? "" : path.replace('\\', '/');
        for (String seg : p.split("/")) {
            if (seg.isEmpty() || ".".equals(seg) || "..".equals(seg)) {
                continue;
            }
            sb.append(encodeSegment(seg)).append('/');
        }
        // 末尾去掉多余的 /
        while (sb.length() > root.length() && sb.charAt(sb.length() - 1) == '/') {
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
        String raw = user + ":" + pass;
        try {
            return "Basic " + Base64.getEncoder().encodeToString(raw.getBytes("UTF-8"));
        } catch (Exception e) {
            return "Basic ";
        }
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
        URL u = new URL(urlFor(path));
        String proto = u.getProtocol();
        int port = u.getPort() > 0 ? u.getPort() : ("https".equals(proto) ? 443 : 80);
        String host = u.getHost();
        String target = u.getFile();
        if (target == null || target.isEmpty()) {
            target = "/";
        }

        Socket sock;
        if ("https".equals(proto)) {
            sock = SSLSocketFactory.getDefault().createSocket();
        } else {
            sock = new Socket();
        }
        try {
            sock.connect(new InetSocketAddress(host, port), TIMEOUT_MS);
            sock.setSoTimeout(TIMEOUT_MS);

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
        Resp r = new Resp();
        String status = readLine(in);
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
        while ((line = readLine(in)) != null && !line.isEmpty()) {
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
            r.body = readChunked(in);
        } else {
            String cl = r.header("content-length");
            int n = 0;
            if (cl != null) {
                try {
                    n = Integer.parseInt(cl.trim());
                } catch (NumberFormatException ignored) {
                }
            }
            r.body = (n > 0) ? readFully(in, n) : new byte[0];
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
        java.io.ByteArrayOutputStream out = new java.io.ByteArrayOutputStream();
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
                break;
            }
            if (size == 0) {
                // 读完 trailer
                String t;
                while ((t = readLine(in)) != null && !t.isEmpty()) {
                    // 忽略
                }
                break;
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
        HttpURLConnection c = open("GET", path, null);
        int code = c.getResponseCode();
        if (code == 404) {
            c.disconnect();
            return null;
        }
        if (code != 200) {
            String err = readAll(c.getErrorStream());
            c.disconnect();
            throw new IOException("WebDAV GET " + path + " -> HTTP " + code
                    + (err.isEmpty() ? "" : " " + err));
        }
        try (InputStream in = c.getInputStream()) {
            return readAllBytes(in);
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
        Resp r = rawRequest("PROPFIND", dir, body.getBytes(StandardCharsets.UTF_8),
                "application/xml; charset=utf-8", "1");
        if (r.code != 207 && r.code != 200) {
            throw new IOException("WebDAV PROPFIND " + dir + " -> HTTP " + r.code
                    + (r.body.length > 0 ? " " + new String(r.body, StandardCharsets.UTF_8) : ""));
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
        h = h.replaceFirst("^/+", "").replaceFirst("/+$", "");
        return h;
    }

    private static String urlDecode(String s) {
        try {
            return java.net.URLDecoder.decode(s, "UTF-8");
        } catch (Exception e) {
            return s;
        }
    }

    /**
     * 递归找一个 PDF（最多 maxDepth 层），用来做「下载链路」自检。
     * 根目录下面通常只有目录, PDF 在 MicroBench工作台/01_Literature/... 里。
     */
    public String findFirstPdf(String dir, int maxDepth) {
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
                String found = findFirstPdf(e.path, maxDepth - 1);
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
            try {
                mkdirs(parent);
            } catch (IOException ignored) {
                // 目录已存在时坚果云会返回 405，忽略即可
            }
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
        java.io.ByteArrayOutputStream bo = new java.io.ByteArrayOutputStream(Math.max(4096, in.available()));
        byte[] buf = new byte[16 * 1024];
        int n;
        while ((n = in.read(buf)) > 0) {
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

        /** 转成 JSON（字段名与网页端约定一致）。 */
        public String toJson() {
            StringBuilder sb = new StringBuilder();
            sb.append('{');
            sb.append("\"path\":").append(jsonStr(path)).append(',');
            sb.append("\"name\":").append(jsonStr(name)).append(',');
            sb.append("\"isDir\":").append(isDir).append(',');
            sb.append("\"size\":").append(size);
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
