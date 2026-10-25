package microbench.tablet;

import android.content.Context;
import android.content.SharedPreferences;

/** 凭证存储。坚果云应用密码与 AI Key 都只留在本机。 */
public class Prefs {

    private static final String FILE = "microbench_tablet";

    private static final String K_DAV_USER = "dav_user";
    private static final String K_DAV_PASS = "dav_pass";
    private static final String K_DAV_ROOT = "dav_root";
    private static final String K_DAV_VAULT = "dav_vault_root";
    private static final String K_DAV_SYNC = "dav_sync_root";

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
     * 统一路径写法：去首尾空白、去首尾斜杠、反斜杠转正斜杠。
     *
     * ⚠ 为什么必须归一化（不是洁癖）：
     *   手填路径可能带尾斜杠「我的坚果云/MicroBench工作台/」，
     *   而探测出来的「我的坚果云/MicroBench工作台」不带。
     *   两者字符串不等 → {@link #redetectVaultRoot()} 每次都认为
     *   「位置变了」→ 每 5 分钟就全盘重探一次 → 白白消耗坚果云配额。
     */
    private static String normalizeRoot(String p) {
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
