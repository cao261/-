package android.content;

/** 内存版的 SharedPreferences 桩（测试用）。 */
public class MemorySharedPreferences implements SharedPreferences {

    /** 直接暴露出来，方便测试预置「配错的前缀」这种状态。 */
    public final java.util.Map<String, String> map =
            new java.util.HashMap<String, String>();

    @Override
    public String getString(String key, String defValue) {
        String v = map.get(key);
        return v == null ? defValue : v;
    }

    @Override
    public Editor edit() {
        return new MemEditor(this);
    }

    /** 具名内部类（不用匿名类 —— AGENTS.md 要求，也不利于调试）。 */
    static class MemEditor implements Editor {
        private final MemorySharedPreferences owner;
        private final java.util.Map<String, String> pending =
                new java.util.HashMap<String, String>();

        MemEditor(MemorySharedPreferences owner) {
            this.owner = owner;
        }

        @Override
        public Editor putString(String key, String value) {
            pending.put(key, value);
            return this;
        }

        @Override
        public void apply() {
            owner.map.putAll(pending);
        }
    }
}
