package microbench.tablet;

import android.content.Context;
import android.content.SharedPreferences;

/** 凭证存储。坚果云应用密码与 AI Key 都只留在本机。 */
public class Prefs {

    private static final String FILE = "microbench_tablet";

    private static final String K_DAV_USER = "dav_user";
    private static final String K_DAV_PASS = "dav_pass";
    private static final String K_DAV_ROOT = "dav_root";

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

    public boolean davConfigured() {
        return !davUser().isEmpty() && !davPass().isEmpty();
    }

    /** 按当前配置造一个 WebDAV 客户端；没配就返回 null。 */
    public DavClient client() {
        if (!davConfigured()) {
            return null;
        }
        return new DavClient(davUser(), davPass(), davRoot());
    }
}
