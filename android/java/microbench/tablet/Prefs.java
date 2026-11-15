package microbench.tablet;

import android.content.Context;
import android.content.SharedPreferences;

import java.util.LinkedHashSet;
import java.util.Set;

/** 凭证存储。坚果云应用密码与 AI Key 都只留在本机。 */
public class Prefs {

    private static final String FILE = "microbench_tablet";

    private static final String K_DAV_USER = "dav_user";
    private static final String K_DAV_PASS = "dav_pass";
    private static final String K_DAV_ROOT = "dav_root";
    private static final String K_DAV_VAULT = "dav_vault_root";
    private static final String K_DAV_SYNC = "dav_sync_root";
    /** 用户自己登记的「AI 网关」主机（逗号分隔）。 */
    private static final String K_AI_HOSTS = "ai_hosts";

    /**
     * 内置的**目标主机白名单**。
     *
     * ⚠ 2026-10-27 审计 C-1：原生桥的 httpPost 原来只校验「https://」前缀
     *   → 页面里任何脚本都能让平板**代自己发请求**（开放代理），
     *   尤其能拿平板当跳板去打用户内网的 https 服务。
     *   → 目标主机必须落在白名单里：
     *     - 坚果云 WebDAV
     *     - 桌面端在用的模型网关（见 microbench/llm.py 的 base_url 默认值）
     *   用户自己填的网关由页面调用 {@link #allowAiHost(String)} 登记。
     */
    public static final String[] DEFAULT_ALLOWED_HOSTS = {
        "dav.jianguoyun.com",     // 坚果云 WebDAV
        "api.minimaxi.com",       // MiniMax（主）
        "api.minimax.io",         // MiniMax（国际站）
        "apihub.agnes-ai.com",    // Agnes 3.0 Flash（备用）
        "api.deepseek.com",       // DeepSeek
        "api.openai.com",         // OpenAI 兼容默认
    };

    private final SharedPreferences sp;

    public Prefs(Context ctx) {
        this.sp = ctx.getApplicationContext()
                .getSharedPreferences(FILE, Context.MODE_PRIVATE);
    }

    public String davUser() {
        return sp.getString(K_DAV_USER, "");
    }

    public String davPass() {
        return sp.getString(K_DAV_PASS, "");
    }

    public String davRoot() {
        return sp.getString(K_DAV_ROOT, DavClient.DEFAULT_ROOT);
    }

    public void setDav(String user, String pass) {
        sp.edit().putString(K_DAV_USER, user == null ? "" : user.trim())
                .putString(K_DAV_PASS, pass == null ? "" : pass).apply();
    }

    /** vault 在云盘里的位置（相对 WebDAV 根），如「我的坚果云/MicroBench工作台」。 */
    public String davVaultRoot() {
        // 读的时候也归一化 —— 旧版本存进去的可能带尾斜杠
        return normalizeRoot(sp.getString(K_DAV_VAULT, ""));
    }

    public void setDavVaultRoot(String p) {
        sp.edit().putString(K_DAV_VAULT, normalizeRoot(p)).apply();
    }

    /**
     * 统一路径写法：去首尾空白、去首尾斜杠、反斜杠转正斜杠、丢掉 . 与 .. 段。
     *
     * ⚠ 为什么必须归一化（不是洁癖）：
     *   手填路径可能带尾斜杠「我的坚果云/MicroBench工作台/」，
     *   而探测出来的「我的坚果云/MicroBench工作台」不带。
     *   两者字符串不等 → {@link #redetectVaultRoot()} 每次都认为
     *   「位置变了」→ 每 5 分钟就全盘重探一次 → 白白消耗坚果云配额。
     *
     * ⚠ 2026-10-27 审计 L-2：". / .." 段也一起丢掉 ——
     *   否则手填「../别的目录」就能让请求越出 vault 根。
     */
    private static String normalizeRoot(String p) {
        if (p == null) {
            return "";
        }
        String s = p.trim().replace('\\', '/');
        StringBuilder sb = new StringBuilder();
        for (String seg : s.split("/")) {
            if (seg.isEmpty() || ".".equals(seg) || "..".equals(seg)) {
                continue;
            }
            if (sb.length() > 0) {
                sb.append('/');
            }
            sb.append(seg);
        }
        return sb.toString();
    }

    /**
     * 同步根在 WebDAV 里的位置（相对 dav 根），例如「我的坚果云/MicroBench同步」。
     *
     * ⚠ 它是 vault 根的**兄弟目录**，不在 vault 里面 ——
     *   桌面端的译文快照和批注都存在这里（跟 PC 端共享的同一份数据）。
     *   平板要拿到译文，就必须能读到这个目录。
     */
    public String davSyncRoot() {
        return normalizeRoot(sp.getString(K_DAV_SYNC, ""));
    }

    public void setDavSyncRoot(String p) {
        sp.edit().putString(K_DAV_SYNC, normalizeRoot(p)).apply();
    }

    /** 同步根的客户端（读译文快照 / 批注）。没配或没配账号时返回 null。 */
    public DavClient syncClient() {
        if (!davConfigured()) {
            return null;
        }
        String sync = davSyncRoot();
        if (sync == null || sync.isEmpty()) {
            return null;
        }
        return new DavClient(davUser(), davPass(), davRoot(), sync);
    }

    public boolean davConfigured() {
        return !davUser().isEmpty() && !davPass().isEmpty();
    }

    /** 按当前配置造一个 WebDAV 客户端；没配就返回 null。 */
    public DavClient client() {
        if (!davConfigured()) {
            return null;
        }
        return new DavClient(davUser(), davPass(), davRoot(), davVaultRoot());
    }

    // ------------------------------------------------------------------
    // AI 网关主机白名单（审计 C-1）
    // ------------------------------------------------------------------

    /**
     * 当前允许的**目标主机**集合（内置 + 用户登记的）。
     */
    public Set<String> aiHosts() {
        Set<String> out = new LinkedHashSet<String>();
        for (String h : DEFAULT_ALLOWED_HOSTS) {
            out.add(h);
        }
        String extra = sp.getString(K_AI_HOSTS, "");
        if (extra != null) {
            for (String h : extra.split(",")) {
                String t = h.trim().toLowerCase();
                if (!t.isEmpty()) {
                    out.add(t);
                }
            }
        }
        return out;
    }

    /**
     * 这个主机能不能作为原生代理的目标。
     *
     * ⚠ 除了「在不在白名单」，还额外要求它是**公网 DNS 名**：
     *   IP 字面量 / localhost / *.local / 单段内网名 一律不收 ——
     *   否则「把平板当跳板打用户内网」照样成立。
     */
    public boolean isAiHostAllowed(String host) {
        if (host == null) {
            return false;
        }
        String h = host.trim().toLowerCase();
        if (h.isEmpty()) {
            return false;
        }
        if (!isPublicHostName(h)) {
            return false;
        }
        return aiHosts().contains(h);
    }

    /**
     * 登记一个「用户自己配置的 AI 网关」主机。
     *
     * ⚠ 页面在用户保存 AI 设置时调用（原生侧不读得到网页的 IndexedDB）。
     *   只收**公网 DNS 名** —— 内网 / IP 字面量一律拒绝。
     *
     * @return 登记成功返回 true
     */
    public boolean allowAiHost(String base) {
        String h = hostOf(base);
        if (h == null || !isPublicHostName(h)) {
            return false;
        }
        Set<String> cur = new LinkedHashSet<String>();
        String extra = sp.getString(K_AI_HOSTS, "");
        if (extra != null) {
            for (String s : extra.split(",")) {
                String t = s.trim().toLowerCase();
                if (!t.isEmpty()) {
                    cur.add(t);
                }
            }
        }
        if (!cur.add(h)) {
            return true;                 // 已经在里面了
        }
        StringBuilder sb = new StringBuilder();
        for (String s : cur) {
            if (sb.length() > 0) {
                sb.append(',');
            }
            sb.append(s);
        }
        sp.edit().putString(K_AI_HOSTS, sb.toString()).apply();
        return true;
    }

    /**
     * 从一个 URL 里取出主机名（只认 https）。
     *
     * ⚠ userinfo（https://user:key@host）必须剥掉 ——
     *   否则主机名会变成 "user:key@host"，白名单永远匹配不上，
     *   而凭据还会跟着一起进日志。
     *
     * @return 主机名；不是 https / 解析不了返回 null
     */
    public static String hostOf(String url) {
        if (url == null) {
            return null;
        }
        String u = url.trim();
        if (!u.toLowerCase().startsWith("https://")) {
            return null;
        }
        String rest = u.substring("https://".length());
        int cut = rest.length();
        for (int i = 0; i < rest.length(); i++) {
            char c = rest.charAt(i);
            if (c == '/' || c == '?' || c == '#') {
                cut = i;
                break;
            }
        }
        String host = rest.substring(0, cut);
        int at = host.lastIndexOf('@');
        if (at >= 0) {
            host = host.substring(at + 1);
        }
        // 去掉端口
        int colon = host.lastIndexOf(':');
        if (colon > 0 && host.indexOf(']') < colon) {
            host = host.substring(0, colon);
        }
        host = host.trim().toLowerCase();
        return host.isEmpty() ? null : host;
    }

    /**
     * 这个主机名是不是**公网 DNS 名**（可以登记为 AI 网关）。
     *
     * ⚠ 判据：
     *   - IP 字面量（IPv4 / IPv6）一律不收 —— 那是「把平板当跳板」的路子；
     *   - localhost / *.local / *.internal / *.lan / *.home.arpa 不收；
     *   - 单段主机名（intranet 这种）不收 —— 它只能在内网解析。
     */
    public static boolean isPublicHostName(String host) {
        if (host == null) {
            return false;
        }
        String h = host.trim().toLowerCase();
        if (h.isEmpty()) {
            return false;
        }
        if (h.startsWith("[") || h.indexOf(':') >= 0) {
            return false;                   // IPv6 字面量
        }
        if (isIpv4Literal(h)) {
            return false;
        }
        if ("localhost".equals(h) || h.endsWith(".localhost")
                || h.endsWith(".local") || h.endsWith(".internal")
                || h.endsWith(".lan") || h.endsWith(".home.arpa")
                || h.endsWith(".intranet")) {
            return false;
        }
        return h.indexOf('.') > 0;         // 至少要有「域名 + 后缀」
    }

    /** 是不是 IPv4 字面量（四个十进制段）。 */
    private static boolean isIpv4Literal(String h) {
        String[] parts = h.split("\\.");
        if (parts.length != 4) {
            return false;
        }
        for (String p : parts) {
            if (p.isEmpty() || p.length() > 3) {
                return false;
            }
            for (int i = 0; i < p.length(); i++) {
                if (p.charAt(i) < '0' || p.charAt(i) > '9') {
                    return false;
                }
            }
            if (Integer.parseInt(p) > 255) {
                return false;
            }
        }
        return true;
    }

    /**
     * vault 位置没配时，**就地探测并存下来**。
     *
     * ⚠ 为什么必须有这个兜底（2026-10-25 用户真机故障）：
     *   vault 的前缀（「我的坚果云/MicroBench工作台」）是**两段**路径，
     *   少了它：
     *     - PROPFIND 打「我的坚果云/01_Literature」→ 服务器返回
     *       **207 + 0 个条目**（不是 404！）→ 网页看到的是「目录是空的」，
     *       于是「已连接 0 篇 · 新增 0」，**一点错都看不出来**；
     *     - GET 打「我的坚果云/01_Literature/xxx.pdf」→ 404 →
     *       24 篇原文一个都下不下来。
     *   而这个前缀只有原生设置页的「测试并保存」会写 —— 用户没跑过、
     *   或者装的是旧版 APK（那时候还没这个字段），就永远是空的。
     *   → 网页侧的「已连接」只看账号密码，**看起来一切正常**。
     *
     * 所以：**运行时就地探测一次**，让用户不必去翻那个藏得很深的原生设置页。
     */
    public synchronized void resolveRootsIfMissing() {
        if (!davConfigured()) {
            return;
        }
        if (!davVaultRoot().isEmpty()) {
            return;                      // 已经配好了，不动它
        }
        try {
            // ⚠ 用**不带前缀**的客户端去探测 —— 探测阶段不能带前缀，
            //   否则会去找「我的坚果云/我的坚果云/MicroBench工作台」。
            DavClient c = new DavClient(davUser(), davPass(), davRoot());
            String v = c.detectVaultRoot();
            if (v == null || v.isEmpty()) {
                return;                  // 探测不到就维持原样（页面会提示）
            }
            setDavVaultRoot(v);
            // 同步根（译文快照 / 批注所在）是 vault 根的兄弟目录
            String s = c.detectSyncRoot(v);
            if (s != null && !s.isEmpty()) {
                setDavSyncRoot(s);
            }
        } catch (Exception e) {
            // 探测失败（没网 / 密码错）→ 维持原样，让页面显示真实状态
        }
    }

    /**
     * **重新**探测 vault 位置（不管原来配的是空还是错）。
     *
     * ⚠ 为什么还要这一层（空的那层不够）：
     *   前面 resolveRootsIfMissing 只在「前缀是空的」时兜底。
     *   但用户也可能**手填错了**前缀（比如云盘里改过目录名），
     *   或者装过某版探测有 bug 的 APK —— 那样前缀是**非空但错的**，
     *   前面的兜底不会触发。
     *   → 列目录结果是空的时候，用这个再探一次：
     *     位置变了就存下来并返回新客户端，没变说明确实是空的 → 返回 null。
     *
     * @return 新客户端（已带上探测到的位置）；没变化或失败返回 null
     */
    public synchronized DavClient redetectVaultRoot() {
        if (!davConfigured()) {
            return null;
        }
        try {
            DavClient c = new DavClient(davUser(), davPass(), davRoot());
            String v = c.detectVaultRoot();
            if (v == null || v.isEmpty()) {
                return null;
            }
            if (v.equals(davVaultRoot())) {
                return null;          // 位置没变 → 没得救，不是路径问题
            }
            setDavVaultRoot(v);
            c.setVaultRoot(v);
            // 同步根（译文快照 / 批注）是 vault 根的**兄弟目录** ——
            // vault 位置变了，同步根也可能跟着变（改名/搬家），
            // 一起刷新，否则译文同步还是坏的。
            String s = c.detectSyncRoot(v);
            if (s != null && !s.isEmpty()) {
                setDavSyncRoot(s);
            }
            return c;
        } catch (Exception e) {
            return null;
        }
    }
}
