package microbench.tablet;

import android.app.Activity;
import android.graphics.Color;
import android.os.Bundle;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

/**
 * 坚果云账号设置页。
 *
 * 为什么要单独一个原生页面：应用密码是**长期凭证**，放进网页就等于暴露给
 * 页面的 JS（哪怕是我们自己的页面）。放在原生侧，网页只能问「配好了没」，
 * 读不到密码本身。
 *
 * UI 用代码构建 —— 手工打包 APK 时不需要任何布局 XML 资源。
 *
 * ⚠️ 不用 lambda、不用匿名内部类（原因见 DavBridge 的注释）。
 */
public class SettingsActivity extends Activity {

    public static final int REQ = 1001;

    private Prefs prefs;
    private EditText userInput;
    private EditText passInput;
    private EditText vaultInput;
    private TextView status;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = new Prefs(this);

        ScrollView scroll = new ScrollView(this);
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(20);
        box.setPadding(pad, dp(16), pad, dp(24));

        TextView title = new TextView(this);
        title.setText("坚果云连接");
        title.setTextSize(22);
        title.setTextColor(Color.parseColor("#1C1A17"));
        box.addView(title, lp(-1, -2, 0, 0, 0, 6));

        TextView hint = new TextView(this);
        hint.setText("让平板直接读写云盘里的 vault 目录，笔记和原文 PDF 才能同步。\n\n"
                + "· 账号：坚果云登录邮箱\n"
                + "· 密码：应用密码（不是账号密码）\n"
                + "  在坚果云网页版 → 设置 → 安全选项 → 应用密码 里生成\n"
                + "· 连接方式：WebDAV");
        hint.setTextSize(13);
        hint.setTextColor(Color.parseColor("#6C6459"));
        hint.setLineSpacing(dp(3), 1f);
        box.addView(hint, lp(-1, -2, 0, 0, 0, 14));

        box.addView(label("账号（邮箱）"), lp(-1, -2, 0, 0, 0, 4));
        userInput = new EditText(this);
        userInput.setInputType(InputType.TYPE_CLASS_TEXT
                | InputType.TYPE_TEXT_VARIATION_EMAIL_ADDRESS);
        userInput.setHint("you@example.com");
        userInput.setText(prefs.davUser());
        userInput.setTextSize(16);
        box.addView(userInput, lp(-1, dp(50), 0, 0, 0, 10));

        box.addView(label("应用密码"), lp(-1, -2, 0, 0, 0, 4));
        passInput = new EditText(this);
        passInput.setInputType(InputType.TYPE_CLASS_TEXT
                | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        passInput.setHint("在坚果云网页版生成的应用密码");
        passInput.setText(prefs.davPass());
        passInput.setTextSize(16);
        box.addView(passInput, lp(-1, dp(50), 0, 0, 0, 12));

        box.addView(label("vault 在云盘里的位置（一般自动探测）"), lp(-1, -2, 0, 0, 0, 4));
        vaultInput = new EditText(this);
        vaultInput.setInputType(InputType.TYPE_CLASS_TEXT);
        vaultInput.setHint("例如：我的坚果云/MicroBench工作台");
        vaultInput.setText(prefs.davVaultRoot());
        vaultInput.setTextSize(15);
        box.addView(vaultInput, lp(-1, dp(50), 0, 0, 0, 12));

        status = new TextView(this);
        status.setTextSize(13);
        status.setTextColor(Color.parseColor("#4F6046"));
        box.addView(status, lp(-1, -2, 0, 0, 0, 8));

        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);

        Button test = new Button(this);
        test.setText("测试并保存");
        test.setOnClickListener(new TestClick(this));
        row.addView(test, new LinearLayout.LayoutParams(0, dp(48), 1f));

        Button save = new Button(this);
        save.setText("保存");
        save.setOnClickListener(new SaveClick(this));
        row.addView(save, new LinearLayout.LayoutParams(0, dp(48), 1f));

        box.addView(row, lp(-1, dp(48), 0, 0, 0, 8));

        TextView back = new TextView(this);
        back.setText("← 返回工作台");
        back.setTextSize(15);
        back.setTextColor(Color.parseColor("#A8482C"));
        back.setPadding(dp(4), dp(12), dp(4), dp(12));
        back.setOnClickListener(new BackClick(this));
        box.addView(back, lp(-1, -2, 0, 0, 0, 0));

        scroll.addView(box);
        setContentView(scroll);
    }

    private void testConnection() {
        status.setText("正在连接…");
        // ⚠ 在**主线程**上把输入框的值取出来。
        //   后台线程再去读 View 是数据竞争（TextView.mText 不是 volatile），
        //   而且下面要改 UI, 跨线程改会抛 CalledFromWrongThreadException。
        final String u = userInput.getText().toString().trim();
        final String p = passInput.getText().toString();
        final String v = vaultInput.getText().toString().trim();
        new Thread(new ProbeTask(this, u, p, v)).start();
    }

    /** 测通了就直接存 —— 之前「测试连接」只测不存, 用户以为配好了,
     *  退出去发现还是「未连接」。 */
    void saveIfOk(String user, String pass, String vaultRoot, String syncRoot) {
        prefs.setDav(user, pass);
        prefs.setDavVaultRoot(vaultRoot);
        if (syncRoot != null && !syncRoot.isEmpty()) {
            prefs.setDavSyncRoot(syncRoot);
        }
    }

    TextView statusView() {
        return status;
    }

    private TextView label(String s) {
        TextView t = new TextView(this);
        t.setText(s);
        t.setTextSize(13);
        t.setTextColor(Color.parseColor("#6C6459"));
        return t;
    }

    private int dp(int v) {
        return (int) (v * getResources().getDisplayMetrics().density + 0.5f);
    }

    private LinearLayout.LayoutParams lp(int w, int h, int l, int t, int r, int b) {
        LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(w, h);
        p.setMargins(dp(l), dp(t), dp(r), dp(b));
        return p;
    }

    // ------------------------------------------------------------------
    // 具名监听器 / 任务（不用 lambda、不用匿名类）
    // ------------------------------------------------------------------

    private static class TestClick implements View.OnClickListener {
        private final SettingsActivity act;

        TestClick(SettingsActivity act) {
            this.act = act;
        }

        @Override
        public void onClick(View v) {
            act.testConnection();
        }
    }

    private static class SaveClick implements View.OnClickListener {
        private final SettingsActivity act;

        SaveClick(SettingsActivity act) {
            this.act = act;
        }

        @Override
        public void onClick(View v) {
            act.prefs.setDav(act.userInput.getText().toString().trim(),
                    act.passInput.getText().toString());
            // vault 位置也一起存 —— 否则下次连的是错的目录。
            act.prefs.setDavVaultRoot(act.vaultInput.getText().toString().trim());
            Toast.makeText(act, "已保存", Toast.LENGTH_SHORT).show();
            act.setResult(RESULT_OK);
            act.finish();
        }
    }

    private static class BackClick implements View.OnClickListener {
        private final SettingsActivity act;

        BackClick(SettingsActivity act) {
            this.act = act;
        }

        @Override
        public void onClick(View v) {
            act.finish();
        }
    }

    /** 后台线程测连接, 完了回主线程更新状态条。 */
    private static class ProbeTask implements Runnable {
        private final SettingsActivity act;
        private final String user;
        private final String pass;
        private final String vault;

        ProbeTask(SettingsActivity act, String user, String pass, String vault) {
            this.act = act;
            this.user = user;
            this.pass = pass;
            this.vault = vault;
        }

        @Override
        public void run() {
            String msg;
            boolean ok;
            // 用户手填的 vault 位置优先；没填就自动探测。
            String detected = vault;
            // 同步根（译文快照 + 批注所在）—— vault 根的兄弟目录。
            // 声明在 try 外面：try 里探测出来的值后面还要用。
            String sync = vault;
            try {
                // 用 prefs 里存的根，而不是写死的 DEFAULT_ROOT ——
                // 否则自定义根根本没法治。
                DavClient c = new DavClient(user, pass, act.prefs.davRoot());

                // ① 先定 vault 在云盘里的位置。
                //    网页给的是 vault 相对路径, 没有这个前缀就会打到
                //    「我的坚果云/01_Literature/...」上 —— 列目录看着正常
                //    (根目录确实有东西), 下载却全部 404。
                if (detected == null || detected.isEmpty()) {
                    detected = c.detectVaultRoot();
                    if (detected == null) {
                        detected = "";
                    }
                }
                c.setVaultRoot(detected);

                // ② 列 vault 目录
                int n = c.list("").size();

                // ③ 同步根 —— 译文快照和批注都在那儿（vault 根的**兄弟目录**）。
                //    ⚠ 没有它，平板就只剩「能看 PDF」，跟用微信直接打开一个 PDF
                //      没有任何区别 —— 译文才是它跟电脑端对等的地方。
                //    ⚠ 必须用**另一个 client**：同一个 client 切 vaultRoot
                //      会把上面刚列好的 vault 目录也换掉。
                if (sync == null || sync.isEmpty()) {
                    sync = c.detectSyncRoot(detected);
                }
                int snaps = 0;
                if (sync != null && !sync.isEmpty()) {
                    try {
                        DavClient sc = new DavClient(user, pass, act.prefs.davRoot(), sync);
                        snaps = sc.list("snapshots").size();
                    } catch (Exception e) {
                        snaps = 0;
                    }
                }

                // ④ 下载链路也得验 —— 只验列目录等于没验下载。
                //    但只取**小文件**的头 64 KB 就够：整篇 PDF 拉下来既慢
                //    （9 MB 走公网）又占内存，还会多发好几个 PROPFIND
                //    （PDF 在 vault 里深达 3 层）—— 那正是把坚果云打到
                //    限流的原因。验链路用几 KB 的 .md 就够。
                String probe;
                boolean dlOk = true;
                String small = c.findSmallestNote("", 2);
                if (small != null) {
                    int got = c.getPartial(small, 64 * 1024);
                    if (got > 4) {
                        probe = "，并成功读到了文件开头（" + got + " 字节）";
                    } else {
                        probe = "；但下载测试没通过（GET 返回空）";
                        dlOk = false;
                    }
                } else {
                    probe = "（没找到 .md，跳过下载测试）";
                }

                ok = dlOk;
                msg = "连接正常，vault 位于「" + (detected.isEmpty() ? "(根目录)" : detected)
                        + "」，笔记 " + n + " 篇"
                        + (sync == null || sync.isEmpty()
                           ? "；⚠ 没找到同步目录（译文/批注没法同步）"
                           : "，同步目录在「" + sync + "」" + (snaps > 0
                               ? "（" + snaps + " 篇译文快照）" : "（暂无译文快照）"))
                        + probe;
            } catch (Exception e) {
                ok = false;
                String m = e.getMessage() == null ? e.toString() : e.getMessage();
                msg = "失败：" + m;
            }
            final String fm = msg;
            final boolean fok = ok;
            final String fvault = detected == null ? "" : detected;
            final String fsync = sync == null ? "" : sync;
            act.runOnUiThread(new StatusTask(act, fm, fok, user, pass, fvault, fsync));
        }
    }

    private static class StatusTask implements Runnable {
        private final SettingsActivity act;
        private final String msg;
        private final boolean ok;
        private final String user;
        private final String pass;
        private final String vault;
        private final String sync;

        StatusTask(SettingsActivity act, String msg, boolean ok,
                   String user, String pass, String vault, String sync) {
            this.act = act;
            this.msg = msg;
            this.ok = ok;
            this.user = user;
            this.pass = pass;
            this.vault = vault;
            this.sync = sync;
        }

        @Override
        public void run() {
            // ⚠ UI 只能在主线程上改。
            //   之前在后台线程里直接 act.vaultInput.setText(...) →
            //   CalledFromWrongThreadException → 被上面的 catch 吞掉 →
            //   判定成「失败」→ saveIfOk 永远不执行 →
            //   **「测试并保存」永远存不进凭证**，这是头号阻断问题。
            if (act.vaultInput != null && !vault.isEmpty()) {
                act.vaultInput.setText(vault);
            }
            TextView s = act.statusView();
            if (s != null) {
                s.setText(msg);
                s.setTextColor(Color.parseColor(ok ? "#4F6046" : "#A8482C"));
            }
            if (ok) {
                // 测通了就把凭证、vault 位置、同步根都存下来, 并直接关掉设置页 ——
                // 用户不需要再点一次「保存」。
                act.saveIfOk(user, pass, vault, sync);
                Toast.makeText(act, "连接正常, 已保存", Toast.LENGTH_SHORT).show();
                act.setResult(RESULT_OK);
                act.finish();
            }
        }
    }
}
