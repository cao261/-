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

        System.out.println("[9] Base64 (自己实现的, 不能依赖 API 26 的 java.util.Base64)");
        String[] b64samples = {
            "", "a", "ab", "abc", "abcd",
            "me@example.com:app-pass-123",
            "中文密码测试",
            "a1b2c3d4e5f6g7h8i9j0",
        };
        boolean b64ok = true;
        StringBuilder b64diff = new StringBuilder();
        for (String s : b64samples) {
            String mine = DavClient.base64(s);
            String ref = java.util.Base64.getEncoder().encodeToString(
                    s.getBytes(java.nio.charset.StandardCharsets.UTF_8));
            if (!mine.equals(ref)) {
                b64ok = false;
                b64diff.append("[").append(s).append("] mine=").append(mine)
                        .append(" ref=").append(ref).append(" ");
            }
        }
        check(b64ok, "自实现 Base64 与 JDK 结果一致 " + b64diff);

        System.out.println("[10] 文件名里的 '+' 不会被解成空格");
        check("C++入门.pdf".equals(DavClient.urlDecode("C%2B%2B%E5%85%A5%E9%97%A8.pdf")),
                "%2B 正确解回 '+'");
        check("C++入门.pdf".equals(DavClient.urlDecode("C++入门.pdf")),
                "字面 '+' 保持不变 (不能变成空格)");
        // 端到端: 服务器把 '+' 原样写在 href 里
        byte[] plus = c.get("我的坚果云/MicroBench工作台/01_Literature/C++入门.pdf");
        check(plus != null && new String(plus, "UTF-8").contains("PDF-PLUS"),
                "带 '+' 的文件能下载到: "
                        + (plus == null ? "(null)" : new String(plus, "UTF-8")));
        boolean sawPlus = false;
        List<DavClient.DavEntry> litFiles =
                c.list("我的坚果云/MicroBench工作台/01_Literature");
        if (litFiles != null) {
            for (DavClient.DavEntry e : litFiles) {
                if (e.name.contains("+")) {
                    sawPlus = true;
                }
            }
        }
        check(sawPlus, "列目录时 '+' 保住了");

        System.out.println("[11] http 根只在回环地址上豁免");
        check(DavClient.isLoopback("http://127.0.0.1:8765/dav/"), "127.0.0.1 算回环");
        check(DavClient.isLoopback("http://localhost:8765/dav/"), "localhost 算回环");
        check(!DavClient.isLoopback("https://dav.jianguoyun.com/dav/"),
                "公网地址不算回环");
        DavClient httpPub = new DavClient("u", "p", "http://dav.jianguoyun.com/dav/");
        check(httpPub.urlFor("x.md").startsWith("https://"),
                "公网的 http 根被强制升级成 https: " + httpPub.urlFor("x.md"));

        System.out.println("[12] 只读文件头 (测下载链路, 不拉整篇)");
        int got = c.getPartial("我的坚果云/MicroBench工作台/00_Dashboard/主工作台看板.md",
                64 * 1024);
        check(got > 0, "getPartial 读到了字节: " + got);
        int tiny = c.getPartial("我的坚果云/MicroBench工作台/00_Dashboard/主工作台看板.md", 4);
        check(tiny == 4, "getPartial 遵守上限: " + tiny);

        System.out.println("[12] 限流退避（坚果云 503 BlockedTemporarily）");
        // 假服务器对 __ratelimit__ 路径第一次回 503，之后正常 ——
        // 客户端必须退避重试，而不是把 503 直接甩给用户。
        boolean rlThrew = false;
        List<DavClient.DavEntry> rl = null;
        long t0 = System.currentTimeMillis();
        try {
            rl = c.list("__ratelimit__");
        } catch (Exception e) {
            rlThrew = true;
            System.out.println("       异常: " + e.getMessage());
        }
        check(!rlThrew, "限流后退避重试成功（没把 503 甩给用户）");
        check(rl != null, "限流重试后拿到了结果");
        long took = System.currentTimeMillis() - t0;
        check(took >= 1000, "重试前确实等了退避时间（" + took + " ms）");

        System.out.println("[13] 目录项带 mtime（用来跳过没变的文件）");
        boolean hasMtime = false;
        for (DavClient.DavEntry e : entries) {
            if (e.mtime > 0) {
                hasMtime = true;
            }
        }
        check(hasMtime, "PROPFIND 的 getlastmodified 被解析成 mtime");
        String j = entries.isEmpty() ? "" : entries.get(0).toJson();
        check(j.contains("\"mtime\""), "DavEntry.toJson 带 mtime 字段");

        System.out.println("[14] 用户真机故障复现：vault 前缀没配");
        // 现象（2026-10-25 用户真机截图）：
        //   - 界面显示「已连接坚果云」（网页只看账号密码对不对）
        //   - 同步结果是「已连接 0 篇 · 新增 0 · 更新 0」
        //   - 24 篇原文 PDF 全部 404
        // 根因：vault 前缀（「我的坚果云/MicroBench工作台」，**两段**）是空的。
        //
        // ⚠⚠ 最阴的地方：前缀错时 PROPFIND **不报 404** ——
        //   服务器回 **207 + 0 个条目**（那个目录本来就不存在，
        //   但 WebDAV 对「不存在的目录」就是这么回）。
        //   → 网页看到的是「目录是空的」，于是显示「已连接 0 篇」，
        //     **一点错都看不出来**，用户只会以为「云盘没同步完」。
        DavClient bare = new DavClient("me@example.com", "app-pass-123", root);
        List<DavClient.DavEntry> wrongList = bare.list("01_Literature");
        check(wrongList != null && wrongList.isEmpty(),
                "前缀为空时列目录**不报错、返回空**（用户看到的「0 篇」）");
        byte[] missing = bare.get("01_Literature/01_论文原文_PDF/a paper.pdf");
        check(missing == null,
                "前缀为空时下载拿不到文件（404）—— 24 篇原文全挂在这里");

        // 自愈路径：探测 → 设前缀 → 重来一次。
        // 这就是原生侧 DavPathHandler 现在做的事（Prefs.resolveRootsIfMissing）。
        String auto = bare.detectVaultRoot();
        check(auto != null && auto.contains("MicroBench工作台"),
                "能自动探测出 vault 位置: " + auto);
        bare.setVaultRoot(auto);
        List<DavClient.DavEntry> fixedList = bare.list("01_Literature");
        check(fixedList != null && !fixedList.isEmpty(),
                "自愈后能列出 vault 内的条目: "
                        + (fixedList == null || fixedList.isEmpty()
                                ? "(空)" : fixedList.get(0).path));
        byte[] okPdf = bare.get("01_Literature/01_论文原文_PDF/a paper.pdf");
        check(okPdf != null && new String(okPdf, "UTF-8").contains("PDF-BYTES"),
                "自愈后能下载 vault 内的 PDF");
        check(fixedList != null && !fixedList.isEmpty()
                        && !fixedList.get(0).path.contains("我的坚果云"),
                "返回的仍是 vault 相对路径（网页传的就是相对路径）");

        System.out.println();
        System.out.println("PASS " + pass + "  FAIL " + fail);
        System.exit(fail == 0 ? 0 : 1);
    }
}
