import java.util.List;

import microbench.tablet.DavClient;

/**
 * 对拍 Java 的 DavClient 与一个 Python 起的最小 WebDAV 服务器。
 *
 * 跑法: python android/_test_dav.py
 * 单独跑: java -cp <classes> TestDav http://127.0.0.1:8765/dav/
 */
public class TestDav {

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

    public static void main(String[] args) throws Exception {
        String root = args.length > 0 ? args[0] : "http://127.0.0.1:8765/dav/";
        DavClient c = new DavClient("me@example.com", "app-pass-123", root);

        System.out.println("[1] URL 构造");
        String u = c.urlFor("我的坚果云/MicroBench工作台/00_Dashboard/主工作台看板.md");
        check(u.contains("%E6%88%91") || u.contains("%E4%B8%BB"),
                "中文路径被百分号编码: " + u);
        check(!u.contains(".."), "路径里的 .. 被剔除");
        check(u.startsWith(root), "URL 以 dav 根开头");
        String u2 = c.urlFor("a b/c&d.pdf");
        check(u2.contains("a%20b") && u2.contains("c%26d"), "空格与 & 被编码: " + u2);

        System.out.println("[2] 列目录 (PROPFIND)");
        List<DavClient.DavEntry> entries = c.list("");
        check(entries != null, "列目录没抛异常");
        boolean sawDir = false;
        for (DavClient.DavEntry e : entries) {
            if (e.isDir) {
                sawDir = true;
            }
        }
        check(sawDir, "根目录下解析出了子目录");

        // 文件在下一层, 根目录 (Depth:1) 本来就只看得到目录
        List<DavClient.DavEntry> files =
                c.list("我的坚果云/MicroBench工作台/00_Dashboard");
        check(files != null && !files.isEmpty(), "下一层列出了条目");
        String fileName = null;
        if (files != null) {
            for (DavClient.DavEntry e : files) {
                if (!e.isDir) {
                    fileName = e.name;
                }
            }
        }
        check(fileName != null && fileName.contains(".md"),
                "文件名带扩展名: " + fileName);
        check(files != null && files.get(0).path.contains("00_Dashboard"),
                "相对路径带目录: " + (files == null || files.isEmpty()
                        ? "(空)" : files.get(0).path));

        System.out.println("[3] 读文件 (GET)");
        byte[] data = c.get("我的坚果云/MicroBench工作台/00_Dashboard/主工作台看板.md");
        check(data != null, "文件读到了");
        check(data != null && new String(data, "UTF-8").contains("看板"),
                "内容里含中文且没乱码");
        check(c.get("不存在/没有这个.md") == null, "不存在的文件返回 null");

        System.out.println("[4] 写文件 (PUT)");
        c.put("我的坚果云/MicroBench工作台/03_Knowledge/新写的.md",
                "# 新笔记\n\n中文内容\n".getBytes("UTF-8"));
        byte[] back = c.get("我的坚果云/MicroBench工作台/03_Knowledge/新写的.md");
        check(back != null, "刚写的文件能读回来");
        check(back != null && new String(back, "UTF-8").contains("中文内容"),
                "写进去的中文没乱码");

        System.out.println("[5] 认证错误");
        DavClient bad = new DavClient("me@example.com", "wrong-pass", root);
        boolean threw = false;
        try {
            bad.list("");
        } catch (Exception e) {
            threw = true;
        }
        check(threw, "密码不对时列目录会失败（而不是静默返回空）");

        System.out.println("[6] JSON 序列化");
        if (entries != null && !entries.isEmpty()) {
            String js = entries.get(0).toJson();
            check(js.startsWith("{") && js.contains("\"path\"") && js.contains("\"isDir\""),
                    "DavEntry.toJson 结构正确: " + js);
        }

        System.out.println("[7] vault 前缀 (网页给的是 vault 相对路径)");
        // 真实场景: vault 在「我的坚果云/MicroBench工作台」下, 网页给的却是
        // 「01_Literature/...」这种相对路径 —— 少了前缀, 列目录看着正常
        // (根目录确实有东西), 但下载全部 404。
        DavClient prefixed = new DavClient("me@example.com", "app-pass-123",
                root, "我的坚果云/MicroBench工作台");
        String pu = prefixed.urlFor("00_Dashboard/主工作台看板.md");
        check(pu.contains("%E6%88%91") || pu.contains("%E4%B8%BB"),
                "前缀里的中文也被编码: " + pu);
        check(pu.contains("dav/") && pu.indexOf("dav/") < pu.indexOf("00_Dashboard"),
                "URL 顺序是 dav根 -> vault前缀 -> 相对路径");

        List<DavClient.DavEntry> pfiles = prefixed.list("00_Dashboard");
        check(pfiles != null && !pfiles.isEmpty(),
                "带前缀能列出 vault 内的笔记: "
                        + (pfiles == null || pfiles.isEmpty() ? "(空)" : pfiles.get(0).path));
        if (pfiles != null && !pfiles.isEmpty()) {
            check(!pfiles.get(0).path.contains("我的坚果云"),
                    "返回的路径是 vault 相对路径 (不含前缀): " + pfiles.get(0).path);
        }
        byte[] pdata = prefixed.get("00_Dashboard/主工作台看板.md");
        check(pdata != null, "带前缀能下载 vault 内的文件");
        check(pdata != null && new String(pdata, "UTF-8").contains("看板"),
                "带前缀下载的内容中文没乱码");

        System.out.println("[8] 自动探测 vault 位置");
        DavClient probe = new DavClient("me@example.com", "app-pass-123", root);
        String detected = probe.detectVaultRoot();
        check(detected != null && detected.contains("MicroBench工作台"),
                "探测出 vault 位置: " + detected);
        if (detected != null) {
            probe.setVaultRoot(detected);
            byte[] dd = probe.get("00_Dashboard/主工作台看板.md");
            check(dd != null, "探测后能下载 vault 内的文件");
        }

        System.out.println();
        System.out.println("PASS " + pass + "  FAIL " + fail);
        System.exit(fail == 0 ? 0 : 1);
    }
}
