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
        return sp.getString(K_DAV_VAULT, "");
    }

    public void setDavVaultRoot(String p) {
        sp.edit().putString(K_DAV_VAULT, p == null ? "" : p.trim()).apply();
    }

    /**
     * 同步根在 WebDAV 里的位置（相对 dav 根），例如「我的坚果云/MicroBench同步」。
     *
     * ⚠ 它是 vault 根的**兄弟目录**，不在 vault 里面 ——
     *   桌面端的译文快照和批注都存在这里（跟 PC 端共享的同一份数据）。
     *   平板要拿到译文，就必须能读到这个目录。
     */
    public String davSyncRoot() {
        return sp.getString(K_DAV_SYNC, "");
    }

    public void setDavSyncRoot(String p) {
        sp.edit().putString(K_DAV_SYNC, p == null ? "" : p.trim()).apply();
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
}
