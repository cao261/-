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

        status = new TextView(this);
        status.setTextSize(13);
        status.setTextColor(Color.parseColor("#4F6046"));
        box.addView(status, lp(-1, -2, 0, 0, 0, 8));

        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);

        Button test = new Button(this);
        test.setText("测试连接");
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
        final String u = userInput.getText().toString();
        final String p = passInput.getText().toString();
        new Thread(new ProbeTask(this, u, p)).start();
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
            act.prefs.setDav(act.userInput.getText().toString(),
                    act.passInput.getText().toString());
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

        ProbeTask(SettingsActivity act, String user, String pass) {
            this.act = act;
            this.user = user;
            this.pass = pass;
        }

        @Override
        public void run() {
            String msg;
            boolean ok;
            try {
                DavClient c = new DavClient(user, pass);
                int n = c.list("").size();
                ok = true;
                msg = "连接正常，云盘根目录有 " + n + " 项";
            } catch (Exception e) {
                ok = false;
                String m = e.getMessage() == null ? e.toString() : e.getMessage();
                msg = "失败：" + m;
            }
            final String fm = msg;
            final boolean fok = ok;
            act.runOnUiThread(new StatusTask(act, fm, fok));
        }
    }

    private static class StatusTask implements Runnable {
        private final SettingsActivity act;
        private final String msg;
        private final boolean ok;

        StatusTask(SettingsActivity act, String msg, boolean ok) {
            this.act = act;
            this.msg = msg;
            this.ok = ok;
        }

        @Override
        public void run() {
            TextView s = act.statusView();
            if (s != null) {
                s.setText(msg);
                s.setTextColor(Color.parseColor(ok ? "#4F6046" : "#A8482C"));
            }
        }
    }
}
