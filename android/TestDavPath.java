import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;

import android.content.Context;
import android.content.MemorySharedPreferences;
import android.webkit.WebResourceResponse;

import microbench.tablet.DavPathHandler;
import microbench.tablet.DavClient;
import microbench.tablet.Prefs;

/**
 * 测的是**真正跑在手机上的那条链路**：DavPathHandler。
 *
 * ⚠ 为什么单独开一个测试文件（而不是并进 TestDav）：
 *   TestDav 测的是 DavClient —— 不依赖 Android，纯 JDK 就能跑。
 *   而 DavPathHandler / Prefs 依赖 Android 框架类，
 *   这里用 android/_stub/ 下的三个桩类把它们垫起来，
 *   于是「自愈逻辑」能在普通 JVM 里被真正执行一遍，
 *   而不是只能靠真机点。
 *
 * ⚠ 这个文件测的正是用户 2026-10-25 的真机故障：
 *   界面显示「已连接」、同步「0 篇」、24 篇 PDF 全 404。
 *
 * 跑法: python android/_test_dav.py
 */
public class TestDavPath {

    private static int pass = 0;
    private static int fail = 0;

    private static void check(boolean cond, String label) {
        if (cond) {
            pass++;
            System.out.println("  PASS  " + label);
        } else {
            fail++;
            System.out.println("  FAIL  " + label);
        }
    }

    /** 把响应体读成字符串。 */
    private static String body(WebResourceResponse r) throws Exception {
        if (r == null || r.data == null) {
            return "";
        }
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        InputStream in = r.data;
        byte[] buf = new byte[4096];
        int n;
        while ((n = in.read(buf)) > 0) {
            bos.write(buf, 0, n);
        }
        return new String(bos.toByteArray(), StandardCharsets.UTF_8);
    }

    /** 数「重探测」被调了几次 —— 用来验证节流（不能每个空目录都全盘扫描）。 */
    static class CountingPrefs extends Prefs {
        int redetectCalls = 0;

        CountingPrefs(Context ctx) {
            super(ctx);
        }

        @Override
        public synchronized DavClient redetectVaultRoot() {
            redetectCalls++;
            return super.redetectVaultRoot();
        }
    }

    /** 造一份「已配好账号」的 Prefs。vaultRoot 传 null 表示没配前缀。 */
    private static CountingPrefs newPrefs(String root, String vaultRoot, String syncRoot) {
        MemorySharedPreferences sp = new MemorySharedPreferences();
        sp.map.put("dav_user", "me@example.com");
        sp.map.put("dav_pass", "app-pass-123");
        sp.map.put("dav_root", root);
        if (vaultRoot != null) {
            sp.map.put("dav_vault_root", vaultRoot);
        }
        if (syncRoot != null) {
            sp.map.put("dav_sync_root", syncRoot);
        }
        return new CountingPrefs(new Context(sp));
    }

    public static void main(String[] args) throws Exception {
        String root = args.length > 0 ? args[0] : "http://127.0.0.1:8765/dav/";

        System.out.println("[15] 空前缀：运行时自愈（用户从没跑过原生设置页）");
        // 症状：界面「已连接」（账号密码对），但同步 0 篇、PDF 全 404。
        // 根因：vault 前缀是空的 —— 只有原生设置页的「测试并保存」会写它。
        CountingPrefs p1 = newPrefs(root, null, null);
        DavPathHandler h1 = new DavPathHandler(p1);
        WebResourceResponse r1 = h1.handle("/dav/list/01_Literature");
        String b1 = body(r1);
        check(r1.statusCode == 200, "列目录返回 200（不是 500）: " + r1.statusCode);
        check(b1.startsWith("[") && !b1.equals("[]"),
                "自愈后列出了条目（不再是空数组）: " + b1);
        check(!p1.davVaultRoot().isEmpty(),
                "前缀已探测并存下来: " + p1.davVaultRoot());
        check(p1.davVaultRoot().contains("MicroBench工作台"),
                "探测到的是正确位置: " + p1.davVaultRoot());

        System.out.println("[16] 错前缀：非空但配错了 → 也要能自愈");
        // 这一层是「空前缀」那层盖不住的：用户手填错、云盘里改过目录名、
        // 或者装过探测有 bug 的旧版 APK —— 前缀非空但是错的。
        CountingPrefs p2 = newPrefs(root, "我的坚果云/老工作台", "我的坚果云/老同步");
        DavPathHandler h2 = new DavPathHandler(p2);
        WebResourceResponse r2 = h2.handle("/dav/list/01_Literature");
        String b2 = body(r2);
        check(r2.statusCode == 200, "列目录返回 200: " + r2.statusCode);
        check(b2.startsWith("[") && !b2.equals("[]"),
                "错前缀也能自愈列出条目: " + b2);
        check(p2.redetectCalls >= 1, "确实触发了一次重探测");
        check("我的坚果云/MicroBench工作台".equals(p2.davVaultRoot()),
                "错前缀被改成了正确位置: " + p2.davVaultRoot());
        check("我的坚果云/MicroBench同步".equals(p2.davSyncRoot()),
                "同步根也跟着刷新了（译文快照要靠它）: " + p2.davSyncRoot());

        System.out.println("[17] 错前缀：下载 PDF（用户看到的 24 篇全 404）");
        CountingPrefs p3 = newPrefs(root, "我的坚果云/老工作台", null);
        DavPathHandler h3 = new DavPathHandler(p3);
        WebResourceResponse r3 =
                h3.handle("/dav/file/01_Literature/01_论文原文_PDF/a paper.pdf");
        String b3 = body(r3);
        check(r3.statusCode == 200, "取文件返回 200（不是 404）: " + r3.statusCode);
        check(b3.contains("PDF-BYTES"), "拿到的正是 vault 里那份 PDF: " + b3);
        check("我的坚果云/MicroBench工作台".equals(p3.davVaultRoot()),
                "下载时也把前缀修对了: " + p3.davVaultRoot());

        System.out.println("[18] 真的是空目录：不能被当成故障，也不能无限重探");
        // 探测要递归整个云盘（好几次 PROPFIND），每个空目录都探一次
        // 几秒就能把坚果云打到限流 → 必须节流。
        CountingPrefs p4 = newPrefs(root, "我的坚果云/MicroBench工作台", null);
        DavPathHandler h4 = new DavPathHandler(p4);
        WebResourceResponse r4 = h4.handle("/dav/list/02_Reproduction");
        String b4 = body(r4);
        check(r4.statusCode == 200, "空目录返回 200: " + r4.statusCode);
        check(b4.equals("[]"), "空目录返回空数组（不是报错）: " + b4);
        check(p4.redetectCalls <= 1,
                "空目录只触发了 1 次重探测（之后被节流）: " + p4.redetectCalls);
        h4.handle("/dav/list/03_Knowledge");
        h4.handle("/dav/list/04_Weekly_Reports");
        check(p4.redetectCalls <= 1,
                "连续多个空目录不会反复全盘扫描: " + p4.redetectCalls);

        System.out.println("[19] 没配账号：给可读的错误，别崩");
        MemorySharedPreferences sp = new MemorySharedPreferences();
        CountingPrefs p5 = new CountingPrefs(new Context(sp));
        DavPathHandler h5 = new DavPathHandler(p5);
        WebResourceResponse r5 = h5.handle("/dav/list/01_Literature");
        String b5 = body(r5);
        check(r5.statusCode == 503, "没配账号返回 503: " + r5.statusCode);
        check(b5.contains("坚果云"), "错误信息说人话: " + b5);

        System.out.println("[20] 兄弟目录陷阱：vault 和同步根名字都含 MicroBench");
        // 真实云盘：
        //   我的坚果云/
        //     ├── MicroBench工作台/   ← vault
        //     └── MicroBench同步/     ← 同步根
        // 坚果云按名字排序 → 「同」在「工」前面 → 同步根**永远**先被遍历到。
        // 只按名字判断的实现，100% 会选错（用户真机就是这么坏的）。
        CountingPrefs p6 = newPrefs(root, null, null);
        DavPathHandler h6 = new DavPathHandler(p6);
        WebResourceResponse r6 = h6.handle("/dav/list/01_Literature");
        String b6 = body(r6);
        check("我的坚果云/MicroBench工作台".equals(p6.davVaultRoot()),
                "自愈选的是 vault，不是它的兄弟目录: " + p6.davVaultRoot());
        check(b6.startsWith("[") && !b6.equals("[]"),
                "自愈后能列出 vault 里的条目: " + b6);
        check("我的坚果云/MicroBench同步".equals(p6.davSyncRoot()),
                "同步根单独定位（译文快照要靠它）: " + p6.davSyncRoot());
        // 同步根里当然没有 01_Literature —— 选错了就必然 0 条
        check(p6.davVaultRoot() != null && !p6.davVaultRoot().equals(p6.davSyncRoot()),
                "vault 和同步根是两个不同的目录: "
                        + p6.davVaultRoot() + " vs " + p6.davSyncRoot());

        System.out.println("[21] 手填路径里的 .. 必须被过滤");
        // ⚠ 2026-10-27 审计 L-2：vault 前缀 / 同步根都是用户手填的，
        //   不过滤 ".." 就能越出 vault 根（删得掉别人的文件）。
        CountingPrefs p7 = newPrefs(root, "我的坚果云/../别的目录", "我的坚果云/../同步");
        check(!p7.davVaultRoot().contains(".."),
                "vault 前缀里的 .. 被过滤: " + p7.davVaultRoot());
        check(!p7.davSyncRoot().contains(".."),
                "同步根里的 .. 被过滤: " + p7.davSyncRoot());

        System.out.println("[22] AI 网关主机白名单（原生代理不能变成开放代理）");
        // ⚠ 2026-10-27 审计 C-1：httpPost 原来只校验 https:// 前缀
        //   → 页面里的脚本能让平板代它发请求（还能拿平板当跳板打内网）。
        CountingPrefs p8 = newPrefs(root, null, null);
        check(p8.isAiHostAllowed("api.minimaxi.com"), "MiniMax 网关在白名单里");
        check(p8.isAiHostAllowed("apihub.agnes-ai.com"), "Agnes 网关在白名单里");
        check(p8.isAiHostAllowed("api.deepseek.com"), "DeepSeek 网关在白名单里");
        check(p8.isAiHostAllowed("dav.jianguoyun.com"), "坚果云在白名单里");
        check(!p8.isAiHostAllowed("evil.example.com"), "陌生域名不在白名单里");
        check(!p8.isAiHostAllowed("192.168.1.10"), "内网 IP 不收");
        check(!p8.isAiHostAllowed("localhost"), "localhost 不收");
        check(p8.allowAiHost("https://my-gw.example.com/v1"),
                "用户登记的公网网关能登记");
        check(p8.isAiHostAllowed("my-gw.example.com"),
                "登记之后就在白名单里: " + p8.aiHosts());
        check(!p8.allowAiHost("http://my-gw2.example.com/v1"), "http 网关不收");
        check(!p8.allowAiHost("https://192.168.1.10/v1"), "内网 IP 不收");
        check(!p8.allowAiHost("https://intranet/v1"), "单段内网名不收");
        check(p8.allowAiHost("https://user:secret@gw.example.com/v1")
                        && p8.isAiHostAllowed("gw.example.com"),
                "带 userinfo 的地址照样登记主机名（凭据不外泄）");
        check(!p8.aiHosts().toString().contains("secret"),
                "白名单里只有主机名，没有凭据: " + p8.aiHosts());

        System.out.println();
        System.out.println("PASS " + pass + "  FAIL " + fail);
        System.exit(fail == 0 ? 0 : 1);
    }
}
