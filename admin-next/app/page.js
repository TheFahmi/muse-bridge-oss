"use client";

import { useCallback, useEffect, useState } from "react";

const API = "/admin/api";
// Dashboard is served from the same host as the bridge.
// (guarded: prerender at build time has no window)
const BASE_URL = typeof window !== "undefined" ? window.location.origin : "";

function authHeaders(token) {
  return { "Content-Type": "application/json", Authorization: "Bearer " + token };
}

async function api(token, p, m, b) {
  const r = await fetch(API + p, {
    method: m || "GET",
    headers: authHeaders(token),
    body: b ? JSON.stringify(b) : undefined,
  });
  if (r.status === 401) throw new Error("unauth");
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error((j.error && j.error.message) || "HTTP " + r.status);
  return j;
}

function fmtDate(s) {
  if (!s) return "–";
  try {
    return new Date(s).toLocaleString("id-ID", {
      timeZone: "Asia/Jakarta", day: "numeric", month: "short",
      hour: "2-digit", minute: "2-digit",
    });
  } catch (e) { return s; }
}

function rel(s) {
  if (!s) return "–";
  const d = (Date.now() - new Date(s).getTime()) / 1e3;
  if (d < 60) return "baru saja";
  if (d < 3600) return Math.floor(d / 60) + " mnt lalu";
  if (d < 86400) return Math.floor(d / 3600) + " jam lalu";
  return Math.floor(d / 86400) + " hari lalu";
}

function reporterPrompt(k) {
  return `Tugas: hubungkan akun Muse di mesin ini ke dashboard terpusat.

Konteks: ada bridge di ${BASE_URL} yang mengelola beberapa akun Muse,
1 akun = 1 API key. Mesin ini login dengan salah satu akun itu. Tugasmu:
laporkan kuota subscription akun ini ke bridge setiap 30 menit.

API key akun di mesin ini:
${k.key}

Langkah:
1. Download reporter:
   curl -sO ${BASE_URL}/admin/report_quota.py
2. Test sekali:
   API_KEY=${k.key} python3 report_quota.py
   Pastikan outputnya "reported: 200". Kalau gagal, cek \`subscription-status
   status\` jalan atau tidak, diagnosa, lalu laporkan ke gue.
3. Pasang cron tiap 30 menit (sesuaikan path dengan \`which python3\` dan
   lokasi file):
   */30 * * * * API_KEY=${k.key} /usr/bin/python3 /path/lengkap/report_quota.py >> /tmp/report_quota.log 2>&1
4. Verifikasi: crontab -l

Laporkan kalau beres + output test langkah 2.`;
}

async function copyText(t) {
  try {
    await navigator.clipboard.writeText(t);
    return true;
  } catch (e) {
    return false;
  }
}

function Brand() {
  return (
    <div className="brand"><i />Muse</div>
  );
}

/* ---------------- login ---------------- */

function LoginView({ onLogin }) {
  const [pw, setPw] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit() {
    const t = pw.trim();
    if (!t) return;
    setBusy(true);
    setErr("");
    try {
      await api(t, "/overview");
      onLogin(t);
    } catch (e) {
      setErr("password salah.");
    }
    setBusy(false);
  }

  return (
    <div className="loginwrap">
      <div className="logincard">
        <Brand />
        <p>Dashboard admin — masukkan password untuk mengelola akun.</p>
        <label htmlFor="pw">Password admin</label>
        <input
          id="pw" type="password" placeholder="••••••••"
          autoComplete="current-password"
          value={pw}
          onChange={(e) => setPw(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") submit(); }}
        />
        <div style={{ height: 12 }} />
        <button className="primary block" onClick={submit} disabled={busy}>
          {busy ? "…" : "Masuk"}
        </button>
        <div className="err">{err}</div>
      </div>
    </div>
  );
}

/* ---------------- account card ---------------- */

function SegBar({ pct }) {
  const p = Math.max(0, Math.min(100, pct));
  return (
    <div className="segbar">
      <i className={"done" + (p >= 90 ? " high" : "")} style={{ width: p + "%" }} />
    </div>
  );
}

function AccountCard({ k, token, onChanged, onUnauth }) {
  const [showFull, setShowFull] = useState(false);
  const [quota, setQuota] = useState(k.quota ?? "");
  const rq = k.real_quota;
  const exh = rq && rq.exhausted;
  const stale = rq && !rq.fresh;

  async function act(a) {
    try {
      if (a === "show") setShowFull((v) => !v);
      else if (a === "copy") {
        (await copyText(k.key)) ? alert("API key disalin") : alert("Gagal menyalin");
      }
      else if (a === "quota") {
        await api(token, "/keys/" + k.id, "PATCH",
          { quota: quota === "" ? null : parseInt(quota) });
        onChanged();
      }
      else if (a === "toggle") {
        await api(token, "/keys/" + k.id, "PATCH", { enabled: !k.enabled });
        onChanged();
      }
      else if (a === "reset") {
        if (confirm(`Reset hitungan pemakaian "${k.name}"?`)) {
          await api(token, "/keys/" + k.id + "/reset", "POST");
          onChanged();
        }
      }
      else if (a === "prompt") {
        (await copyText(reporterPrompt(k)))
          ? alert(`Prompt disalin — kirim ke Muse di mesin akun "${k.name}"`)
          : alert("Gagal menyalin");
      }
      else if (a === "del") {
        if (confirm(`Hapus akun "${k.name}"? Key-nya langsung mati.`)) {
          await api(token, "/keys/" + k.id, "DELETE");
          onChanged();
        }
      }
    } catch (e) {
      if (e.message === "unauth") onUnauth();
      else alert("Gagal: " + e.message);
    }
  }

  return (
    <div className={"acct" + (k.enabled ? "" : " disabled")}>
      <div className="acct-head">
        <div className="acct-name"><span className="dot" />{k.name}</div>
        <span className={"tag " + (k.enabled ? "ok" : "bad")}>
          {k.enabled ? "AKTIF" : "NONAKTIF"}
        </span>
      </div>

      <div className="keybox">
        <code>{showFull ? k.key : k.key_masked}</code>
        <button className="small" onClick={() => act("show")}>
          {showFull ? "sembunyi" : "tampil"}
        </button>

      </div>

      {rq ? (
        <>
          <div className="planrow">
            <span className="plan">{rq.plan || "–"}</span>
            {exh ? <span className="tag bad">LIMIT HABIS</span>
              : stale ? <span className="tag warn">DATA BASI</span>
              : <span className="tag ok">KUOTA OK</span>}
          </div>
          <div className="kv"><span className="k">weekly limit</span>
            <span className="v">{rq.weekly_used_pct}% terpakai</span></div>
          <SegBar pct={rq.weekly_used_pct} />
          <div className="kv"><span className="k">reset</span>
            <span className="v">{fmtDate(rq.weekly_resets_at)}</span></div>
          <div className="kv"><span className="k">additional</span>
            <span className="v">{rq.additional_used_pct ?? "–"}% used</span></div>
          <div className="kv"><span className="k">update</span>
            <span className="v">{rel(rq.updated_at)}{stale ? <> · <span className="stale">reporter &gt;3 jam</span></> : null}</span></div>
        </>
      ) : (
        <div className="meta">belum ada laporan kuota real — kirim prompt reporter ke Muse di mesin akun ini.</div>
      )}

      <div className="kv" style={{ marginTop: 8 }}>
        <span className="k">request via bridge</span>
        <span className="v">{k.used.toLocaleString("id-ID")}{k.quota != null ? " / " + k.quota.toLocaleString("id-ID") : ""}</span>
      </div>

      <div className="qedit">
        <input type="number" value={quota} placeholder="kuota manual" min="0"
          onChange={(e) => setQuota(e.target.value)} />
        <button className="small" onClick={() => act("quota")}>simpan</button>
      </div>

      <div className="actions">
        <button className="small" onClick={() => act("toggle")}>{k.enabled ? "nonaktifkan" : "aktifkan"}</button>
        <button className="small" onClick={() => act("reset")}>reset pakai</button>
        <button className="small" onClick={() => act("prompt")}>prompt reporter</button>
        <button className="small danger" onClick={() => act("del")}>hapus</button>
      </div>

      <div className="meta">
        dibuat {fmtDate(k.created_at)}{k.last_used_at ? " · terakhir dipakai " + rel(k.last_used_at) : ""}
      </div>
    </div>
  );
}

/* ---------------- views ---------------- */

function AkunView({ ov, token, onChanged, onUnauth }) {
  if (!ov) return <div className="page"><div className="empty">memuat…</div></div>;
  const ex = ov.keys.filter((k) => k.real_quota && k.real_quota.exhausted).length;
  return (
    <div className="page">
      <div className="stats">
        <div className="stat"><div className="n">{ov.total_keys}</div><div className="l">Total akun</div></div>
        <div className="stat"><div className="n">{ov.enabled_keys}</div><div className="l">Aktif</div></div>
        <div className="stat"><div className="n">{ov.total_used.toLocaleString("id-ID")}</div><div className="l">Request terjawab</div></div>
        <div className="stat"><div className={"n" + (ex ? " warn" : "")}>{ex}</div><div className="l">Limit habis</div></div>
      </div>
      <div className="sechead"><h2>Akun</h2><span>{ov.keys.length} akun</span></div>
      <div className="grid">
        {ov.keys.length === 0
          ? <div className="empty">belum ada akun — tambah di halaman <b>+ tambah</b>.</div>
          : ov.keys.map((k) => (
            <AccountCard key={k.id} k={k} token={token} onChanged={onChanged} onUnauth={onUnauth} />
          ))}
      </div>
    </div>
  );
}

function TambahView({ token, onCreated, onUnauth }) {
  const [name, setName] = useState("");
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit() {
    const n = name.trim();
    if (!n) { alert("Isi nama akun dulu"); return; }
    setBusy(true);
    try {
      const d = await api(token, "/keys", "POST",
        { name: n, quota: q === "" ? null : parseInt(q) });
      setName(""); setQ("");
      (await copyText(reporterPrompt(d.key))) || alert("Gagal menyalin prompt otomatis");
      onCreated(d.key);
    } catch (e) {
      if (e.message === "unauth") onUnauth();
      else alert("Gagal: " + e.message);
    }
    setBusy(false);
  }

  return (
    <div className="page">
      <div className="sechead"><h2>Tambah akun</h2></div>
      <div className="panel">
        <h3>Cara tambah akun</h3>
        <p className="desc">Satu akun Muse = satu API key. Kuota mengikuti subscription asli akun itu.</p>
        <ol className="steps">
          <li><div>Isi nama akun di form bawah, lalu klik <b>Buat akun</b>. Satu API key langsung dibuat untuk akun ini.</div></li>
          <li><div><b>Prompt reporter otomatis disalin</b> ke clipboard — kirim ke Muse di mesin tempat akun itu login.</div></li>
          <li><div>Muse di mesin itu akan download reporter, test sekali (<code>reported: 200</code>), lalu pasang cron tiap 30 menit.</div></li>
          <li><div>Selesai — kuota real akun muncul di dashboard dan ikut mengendalikan limit.</div></li>
        </ol>
        <div style={{ margin: "4px 0 20px" }}>
          <label>Base URL</label>
          <div className="keybox"><code>{BASE_URL}</code>
            <button className="small" onClick={async () =>
              (await copyText(BASE_URL)) ? alert("Base URL disalin") : alert("Gagal menyalin")}>
              salin
            </button></div>
          <div className="hint">Semua request API dikirim ke base URL ini.</div>
        </div>
        <table>
          <tbody>
            <tr><th style={{ width: 130 }}>Fungsi</th><th>Endpoint</th></tr>
            <tr><td>Chat</td><td><code>POST /v1/chat/completions</code></td></tr>
            <tr><td>Model</td><td><code>GET /v1/models</code></td></tr>
            <tr><td>Kuota real</td><td><code>GET /v1/real-quota</code></td></tr>
            <tr><td>Lapor kuota</td><td><code>POST /v1/real-quota</code></td></tr>
          </tbody>
        </table>
      </div>
      <div className="panel">
        <h3>Akun baru</h3>
        <p className="desc">Setelah dibuat, API key dan prompt reporter akan ditampilkan.</p>
        <div className="frow">
          <div><label htmlFor="newName">Nama akun</label>
            <input type="text" id="newName" placeholder="cth: Muse Pribadi" maxLength={60}
              value={name} onChange={(e) => setName(e.target.value)} /></div>
          <div><label htmlFor="newQuota">Kuota manual <span style={{ color: "var(--faint)", fontWeight: 400 }}>(opsional)</span></label>
            <input type="number" id="newQuota" placeholder="cth: 10000" min="0"
              value={q} onChange={(e) => setQ(e.target.value)} /></div>
        </div>
        <button className="primary block" onClick={submit} disabled={busy}>
          {busy ? "…" : "Buat akun"}
        </button>
        <div className="hint">Kosongkan kuota manual kalau mau mengandalkan kuota subscription asli saja.</div>
      </div>
    </div>
  );
}

function PengaturanView({ token, onUnauth }) {
  const [a, setA] = useState("");
  const [b, setB] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit() {
    if (a.length < 8) { alert("Minimal 8 karakter"); return; }
    if (a !== b) { alert("Password tidak sama"); return; }
    if (!confirm("Ganti password admin? Lu harus login ulang.")) return;
    setBusy(true);
    try {
      await api(token, "/password", "POST", { password: a });
      alert("Password diganti. Login lagi pakai password baru.");
      localStorage.removeItem("muse_admin_token");
      location.reload();
    } catch (e) {
      if (e.message === "unauth") onUnauth();
      else alert("Gagal: " + e.message);
    }
    setBusy(false);
  }

  return (
    <div className="page">
      <div className="sechead"><h2>Pengaturan</h2></div>
      <div className="panel">
        <h3>Ganti password admin</h3>
        <p className="desc">Minimal 8 karakter. Setelah diganti, lu harus login ulang.</p>
        <div className="vstack">
          <div><label htmlFor="newPw">Password baru</label>
            <input type="password" id="newPw" placeholder="minimal 8 karakter"
              autoComplete="new-password" value={a} onChange={(e) => setA(e.target.value)} /></div>
          <div><label htmlFor="newPw2">Ulangi password</label>
            <input type="password" id="newPw2" placeholder="ulangi password baru"
              autoComplete="new-password" value={b} onChange={(e) => setB(e.target.value)} /></div>
          <button className="primary" onClick={submit} disabled={busy}>
            {busy ? "…" : "Ganti password"}
          </button>
        </div>
      </div>
    </div>
  );
}

function NewKeyModal({ data, onClose }) {
  return (
    <div className="modalwrap" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h3>Akun &ldquo;{data.name}&rdquo; dibuat</h3>
        <p>Prompt reporter <b>sudah disalin otomatis</b> — tinggal kirim ke Muse di mesin akun itu.</p>
        <div>
          <label>API key</label>
          <div className="keybox"><code>{data.key}</code>
            <button className="small" onClick={async () =>
              (await copyText(data.key)) ? alert("API key disalin") : alert("Gagal menyalin")}>
              salin
            </button></div>
        </div>
        <div className="row">
          <button className="small" onClick={async () =>
            (await copyText(reporterPrompt(data))) ? alert("Prompt disalin") : alert("Gagal menyalin")}>
            salin prompt lagi
          </button>
          <button className="small primary" onClick={onClose}>selesai</button>
        </div>
      </div>
    </div>
  );
}

/* ---------------- main ---------------- */

export default function Admin() {
  const [mounted, setMounted] = useState(false);
  const [token, setToken] = useState("");
  const [authed, setAuthed] = useState(false);
  const [page, setPageState] = useState("akun");
  const [ov, setOv] = useState(null);
  const [newKey, setNewKey] = useState(null);
  const [foot, setFoot] = useState("");

  const logout = useCallback(() => {
    localStorage.removeItem("muse_admin_token");
    location.reload();
  }, []);

  const load = useCallback(async (t) => {
    try {
      const d = await api(t, "/overview");
      setOv(d);
      setFoot("auto-refresh 30 detik · " +
        new Date().toLocaleTimeString("id-ID", { timeZone: "Asia/Jakarta" }) + " WIB");
    } catch (e) {
      if (e.message === "unauth") logout();
    }
  }, [logout]);

  useEffect(() => {
    setMounted(true);
    const p = localStorage.getItem("muse_admin_page") || "akun";
    setPageState(p);
    const t = localStorage.getItem("muse_admin_token") || "";
    if (t) {
      setToken(t);
      api(t, "/overview")
        .then(() => {
          localStorage.setItem("muse_admin_token", t);
          setAuthed(true);
          load(t);
        })
        .catch(() => { /* stay on login */ });
    }
  }, [load]);

  useEffect(() => {
    if (!authed || !token) return;
    const id = setInterval(() => load(token), 30000);
    return () => clearInterval(id);
  }, [authed, token, load]);

  function setPage(p) {
    setPageState(p);
    try { localStorage.setItem("muse_admin_page", p); } catch (e) {}
  }

  function handleLogin(t) {
    localStorage.setItem("muse_admin_token", t);
    setToken(t);
    setAuthed(true);
    load(t);
  }

  if (!mounted) return null;
  if (!authed) return <LoginView onLogin={handleLogin} />;

  const pages = ["akun", "tambah", "pengaturan"];
  const labels = { akun: "Akun", tambah: "+ Tambah", pengaturan: "Pengaturan" };

  return (
    <>
      <header>
        <Brand />
        <nav>
          {pages.map((p) => (
            <button key={p} className={page === p ? "active" : ""}
              onClick={() => setPage(p)}>
              {labels[p]}
            </button>
          ))}
        </nav>
        <button className="linkbtn" onClick={logout}>Keluar</button>
      </header>

      {page === "akun" && (
        <AkunView ov={ov} token={token}
          onChanged={() => load(token)} onUnauth={logout} />
      )}
      {page === "tambah" && (
        <TambahView token={token}
          onCreated={(k) => { setNewKey(k); setPage("akun"); load(token); }}
          onUnauth={logout} />
      )}
      {page === "pengaturan" && (
        <PengaturanView token={token} onUnauth={logout} />
      )}

      <footer>{foot}</footer>

      {newKey && <NewKeyModal data={newKey} onClose={() => setNewKey(null)} />}
    </>
  );
}
