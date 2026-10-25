package android.content;

/**
 * 测试用的 android.content.Context 桩。
 *
 * 只保留 Prefs 用得到的那几个方法：
 *   getApplicationContext() / getSharedPreferences(String, int)
 */
public class Context {

    public static final int MODE_PRIVATE = 0;

    private final SharedPreferences prefs;

    public Context(SharedPreferences prefs) {
        this.prefs = prefs;
    }

    public Context getApplicationContext() {
        return this;
    }

    public SharedPreferences getSharedPreferences(String name, int mode) {
        return prefs;
    }
}
