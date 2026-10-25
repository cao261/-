package android.content;

/**
 * 测试用的 android.content.SharedPreferences 桩。
 *
 * ⚠ 为什么需要它：
 *   真机上没法自动测 WebDAV，所以用 Python 起一个假服务器让 Java 连上来跑。
 *   但 {@link microbench.tablet.Prefs} 和 {@link microbench.tablet.DavPathHandler}
 *   依赖 Android 框架类（Context / SharedPreferences / WebResourceResponse），
 *   在普通 JDK 下编译不了。
 *   → 这里手写三个最小桩类，让「自愈逻辑」能在普通 JVM 里被真正跑一遍。
 *
 * ⚠ 这些桩**只进测试**，不进 APK：
 *   build_apk.ps1 只编译 android/java 与 android/gen，
 *   而桩放在 android/_stub/ 下 —— 绝不能挪进 android/java，
 *   否则会跟真 android.jar 撞类，APK 直接编不过。
 */
public interface SharedPreferences {

    String getString(String key, String defValue);

    Editor edit();

    /** 只实现了桩测试用得到的那几个方法。 */
    interface Editor {
        Editor putString(String key, String value);

        void apply();
    }
}
