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
}
