// ---------- helpers ----------
const $ = (id) => document.getElementById(id);
let pendingEmail = "";
let me = null;

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), 3000);
}

async function api(path, body, method) {
  const opts = {
    method: method || (body ? "POST" : "GET"),
    credentials: "include",
    headers: {},
  };
  if (body) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = {};
  try { data = await res.json(); } catch (e) { /* non-json */ }
  if (!res.ok) {
    const err = new Error(data.error || "Request failed");
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

async function apiUpload(path, formData) {
  const res = await fetch(path, { method: "POST", credentials: "include", body: formData });
  const data = await res.json();
  if (!res.ok) { const err = new Error(data.error || "Upload failed"); err.data = data; throw err; }
  return data;
}

// ---------- auth screen switching ----------
function showAuthForm(name) {
  ["loginForm", "registerForm", "otpForm", "forgotForm", "resetForm"].forEach((id) => {
    $(id).classList.toggle("hidden", id !== name);
  });
  $("authError").classList.add("hidden");
  $("authOk").classList.add("hidden");
}
function authError(msg) {
  $("authOk").classList.add("hidden");
  $("authError").textContent = msg;
  $("authError").classList.remove("hidden");
}
function authOk(msg) {
  $("authError").classList.add("hidden");
  $("authOk").textContent = msg;
  $("authOk").classList.remove("hidden");
}

$("gotoRegister").onclick = (e) => { e.preventDefault(); showAuthForm("registerForm"); };
$("gotoLogin1").onclick = (e) => { e.preventDefault(); showAuthForm("loginForm"); };
$("gotoLogin2").onclick = (e) => { e.preventDefault(); showAuthForm("loginForm"); };
$("gotoForgot").onclick = (e) => { e.preventDefault(); showAuthForm("forgotForm"); };

$("loginForm").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/api/login", {
      email: $("loginEmail").value.trim(),
      password: $("loginPassword").value,
      remember_me: $("loginRemember").checked,
    });
    await enterApp();
  } catch (err) {
    if (err.status === 403) {
      pendingEmail = $("loginEmail").value.trim();
      $("otpEmailLabel").textContent = pendingEmail;
      showAuthForm("otpForm");
    } else {
      authError(err.message);
    }
  }
};

$("registerForm").onsubmit = async (e) => {
  e.preventDefault();
  if ($("regPassword").value !== $("regConfirm").value) {
    authError("Passwords don't match."); return;
  }
  try {
    const data = await api("/api/register", {
      name: $("regName").value.trim(),
      email: $("regEmail").value.trim(),
      password: $("regPassword").value,
      confirm_password: $("regConfirm").value,
    });
    pendingEmail = $("regEmail").value.trim();
    if (data.skip_otp) {
      authOk("Account created — you can log in now.");
      showAuthForm("loginForm");
    } else {
      $("otpEmailLabel").textContent = pendingEmail;
      showAuthForm("otpForm");
      if (data.warning) toast(data.warning);
    }
  } catch (err) { authError(err.message); }
};

$("otpForm").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/api/verify-otp", { email: pendingEmail, otp: $("otpCode").value.trim() });
    authOk("Verified! You can log in now.");
    showAuthForm("loginForm");
  } catch (err) { authError(err.message); }
};
$("resendOtp").onclick = async (e) => {
  e.preventDefault();
  try { await api("/api/resend-otp", { email: pendingEmail }); toast("New code sent."); }
  catch (err) { authError(err.message); }
};

$("forgotForm").onsubmit = async (e) => {
  e.preventDefault();
  try {
    pendingEmail = $("forgotEmail").value.trim();
    await api("/api/password/forgot", { email: pendingEmail });
    $("resetEmailLabel").textContent = pendingEmail;
    showAuthForm("resetForm");
  } catch (err) { authError(err.message); }
};

$("resetForm").onsubmit = async (e) => {
  e.preventDefault();
  if ($("resetNewPw").value !== $("resetConfirmPw").value) { authError("Passwords don't match."); return; }
  try {
    await api("/api/password/reset", {
      email: pendingEmail, otp: $("resetCode").value.trim(),
      new_password: $("resetNewPw").value, confirm_password: $("resetConfirmPw").value,
    });
    authOk("Password reset. Log in with your new password.");
    showAuthForm("loginForm");
  } catch (err) { authError(err.message); }
};

// ---------- app shell ----------
async function enterApp() {
  me = await api("/api/me");
  if (!me.logged_in) { showAuthForm("loginForm"); return; }
  $("authScreen").classList.add("hidden");
  $("app").classList.remove("hidden");
  $("dashName").textContent = me.name;
  $("statXp").textContent = me.xp;
  $("statMinutes").textContent = me.study_minutes;
  switchView("dashboard");
}

document.querySelectorAll(".nav-item[data-view]").forEach((btn) => {
  btn.onclick = () => switchView(btn.dataset.view);
});

function switchView(name) {
  document.querySelectorAll(".view").forEach((v) => v.classList.add("hidden"));
  document.querySelectorAll(".nav-item[data-view]").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  $("view-" + name).classList.remove("hidden");
  const loaders = {
    notes: loadNotes, tasks: loadTasks, courses: loadCourses, flip: loadFlashcards,
    quiz: loadQuiz, groups: loadGroups, glory: loadLeaderboard, sources: loadSources,
    dopa: loadChat,
  };
  if (loaders[name]) loaders[name]();
}

$("logoutBtn").onclick = async () => {
  await api("/api/logout");
  $("app").classList.add("hidden");
  $("authScreen").classList.remove("hidden");
  showAuthForm("loginForm");
};

$("proBtn").onclick = async () => {
  try { await api("/api/upgrade/init", {}); }
  catch (err) { toast(err.message); }
};

// ---------- Notes ----------
async function loadNotes() {
  const notes = await api("/api/notes");
  $("notesList").innerHTML = notes.map((n) => `
    <div class="item-card"><h4>${escapeHtml(n.title)}</h4><p>${escapeHtml(n.content)}</p></div>
  `).join("") || `<p class="hint">No notes yet — generate some with AI above, or add your own.</p>`;
  await fillSourceDropdown("noteGenSource");
}
$("addNoteBtn").onclick = async () => {
  const title = $("noteTitle").value.trim(), content = $("noteContent").value.trim();
  if (!title) return toast("Give the note a title.");
  await api("/api/notes", { title, content });
  $("noteTitle").value = ""; $("noteContent").value = "";
  loadNotes();
};
$("noteGenBtn").onclick = async () => {
  const topic = $("noteGenTopic").value.trim(), source_id = $("noteGenSource").value;
  if (!topic && !source_id) return toast("Enter a topic or pick a source.");
  $("noteGenBtn").textContent = "Generating..."; $("noteGenBtn").disabled = true;
  try {
    const body = source_id ? { source_id: Number(source_id) } : { topic };
    await api("/api/notes/generate", body);
    $("noteGenTopic").value = "";
    toast("Notes generated.");
    loadNotes();
  } catch (err) { toast(err.message); }
  finally { $("noteGenBtn").textContent = "Generate"; $("noteGenBtn").disabled = false; }
};

// ---------- Tasks ----------
async function loadTasks() {
  const tasks = await api("/api/tasks");
  $("tasksList").innerHTML = tasks.map((t) => `
    <div class="item-card">
      <h4>${escapeHtml(t.title)} <span class="item-meta">· ${t.priority} · ${escapeHtml(t.deadline)}</span></h4>
      <button class="btn" data-toggle="${t.id}">${t.status}</button>
    </div>
  `).join("") || `<p class="hint">No tasks yet.</p>`;
  document.querySelectorAll("[data-toggle]").forEach((b) => {
    b.onclick = async () => { await api(`/api/tasks/${b.dataset.toggle}/toggle`, {}); loadTasks(); };
  });
}
$("addTaskBtn").onclick = async () => {
  const title = $("taskTitle").value.trim();
  if (!title) return toast("Enter a task title.");
  await api("/api/tasks", { title, priority: $("taskPriority").value, deadline: $("taskDeadline").value.trim() });
  $("taskTitle").value = ""; $("taskDeadline").value = "";
  loadTasks();
};

// ---------- Courses ----------
async function loadCourses() {
  const courses = await api("/api/courses");
  $("coursesList").innerHTML = courses.map((c) => `<div class="item-card"><h4>${escapeHtml(c.name)}</h4></div>`).join("") || `<p class="hint">No courses yet.</p>`;
}
$("addCourseBtn").onclick = async () => {
  const name = $("courseName").value.trim();
  if (!name) return;
  await api("/api/courses", { name });
  $("courseName").value = "";
  loadCourses();
};

// ---------- Focus timer ----------
let focusSeconds = 25 * 60, focusTimer = null, focusRunning = false;
function renderClock() {
  const m = String(Math.floor(focusSeconds / 60)).padStart(2, "0");
  const s = String(focusSeconds % 60).padStart(2, "0");
  $("focusClock").textContent = `${m}:${s}`;
}
$("focusStart").onclick = () => {
  if (focusRunning) { clearInterval(focusTimer); focusRunning = false; $("focusStart").textContent = "Start"; return; }
  focusRunning = true; $("focusStart").textContent = "Pause";
  focusTimer = setInterval(async () => {
    focusSeconds--;
    renderClock();
    if (focusSeconds <= 0) {
      clearInterval(focusTimer); focusRunning = false; $("focusStart").textContent = "Start";
      const res = await api("/api/focus/complete", { minutes: 25 });
      toast(`Session complete! +${res.xp_gain} XP`);
      focusSeconds = 25 * 60; renderClock();
    }
  }, 1000);
};
$("focusReset").onclick = () => {
  clearInterval(focusTimer); focusRunning = false; $("focusStart").textContent = "Start";
  focusSeconds = 25 * 60; renderClock();
};
renderClock();

// ---------- Flashcards ----------
let flipCards = [], flipIndex = 0, flipShowingBack = false;
async function loadFlashcards() {
  flipCards = await api("/api/flashcards");
  $("cardsList").innerHTML = flipCards.map((c) => `
    <div class="item-card"><h4>${escapeHtml(c.front)}</h4><p>${escapeHtml(c.back)}</p></div>
  `).join("") || `<p class="hint">No flashcards yet — generate some with AI above.</p>`;
  await fillSourceDropdown("cardGenSource");
}
$("addCardBtn").onclick = async () => {
  const front = $("cardFront").value.trim(), back = $("cardBack").value.trim();
  if (!front || !back) return toast("Fill in both sides.");
  await api("/api/flashcards", { front, back });
  $("cardFront").value = ""; $("cardBack").value = "";
  loadFlashcards();
};
$("cardGenBtn").onclick = async () => {
  const topic = $("cardGenTopic").value.trim(), source_id = $("cardGenSource").value;
  if (!topic && !source_id) return toast("Enter a topic or pick a source.");
  $("cardGenBtn").textContent = "Generating..."; $("cardGenBtn").disabled = true;
  try {
    const body = source_id ? { source_id: Number(source_id), count: Number($("cardGenCount").value) } : { topic, count: Number($("cardGenCount").value) };
    const res = await api("/api/flashcards/generate", body);
    toast(`${res.count} flashcards generated.`);
    loadFlashcards();
  } catch (err) { toast(err.message); }
  finally { $("cardGenBtn").textContent = "Generate"; $("cardGenBtn").disabled = false; }
};
$("showFlipBtn").onclick = () => {
  if (!flipCards.length) return toast("No flashcards yet.");
  flipIndex = 0; flipShowingBack = false;
  $("flipViewer").classList.remove("hidden");
  renderFlashcard();
};
function renderFlashcard() {
  const c = flipCards[flipIndex];
  $("flashcardEl").textContent = flipShowingBack ? c.back : c.front;
}
$("flipFlip").onclick = () => { flipShowingBack = !flipShowingBack; renderFlashcard(); };
$("flipNext").onclick = () => { flipIndex = (flipIndex + 1) % flipCards.length; flipShowingBack = false; renderFlashcard(); };
$("flipPrev").onclick = () => { flipIndex = (flipIndex - 1 + flipCards.length) % flipCards.length; flipShowingBack = false; renderFlashcard(); };

// ---------- Quiz ----------
async function loadQuiz() {
  const questions = await api("/api/quiz");
  $("quizList").innerHTML = questions.map((q, i) => `
    <div class="item-card">
      <h4>${escapeHtml(q.question)}</h4>
      ${q.options.map((opt) => `
        <label style="display:block;margin:4px 0;font-size:14px;">
          <input type="radio" name="q${q.id}" value="${escapeHtml(opt)}"> ${escapeHtml(opt)}
        </label>`).join("")}
      <button class="btn" data-check="${q.id}" data-correct="${escapeHtml(q.correct)}">Check answer</button>
      <p class="item-meta" data-result="${q.id}"></p>
    </div>
  `).join("") || `<p class="hint">No quiz questions yet — generate some with AI above.</p>`;
  document.querySelectorAll("[data-check]").forEach((b) => {
    b.onclick = () => {
      const qid = b.dataset.check, correct = b.dataset.correct;
      const picked = document.querySelector(`input[name="q${qid}"]:checked`);
      const result = document.querySelector(`[data-result="${qid}"]`);
      if (!picked) { result.textContent = "Pick an option first."; return; }
      result.textContent = picked.value === correct ? "Correct!" : `Incorrect — the answer is ${correct}.`;
    };
  });
  await fillSourceDropdown("quizGenSource");
}
$("quizGenBtn").onclick = async () => {
  const topic = $("quizGenTopic").value.trim(), source_id = $("quizGenSource").value;
  if (!topic && !source_id) return toast("Enter a topic or pick a source.");
  $("quizGenBtn").textContent = "Generating..."; $("quizGenBtn").disabled = true;
  try {
    const body = source_id ? { source_id: Number(source_id), count: Number($("quizGenCount").value) } : { topic, count: Number($("quizGenCount").value) };
    const res = await api("/api/quiz/generate", body);
    toast(`${res.count} questions generated.`);
    loadQuiz();
  } catch (err) { toast(err.message); }
  finally { $("quizGenBtn").textContent = "Generate"; $("quizGenBtn").disabled = false; }
};

// ---------- Groups ----------
async function loadGroups() {
  const groups = await api("/api/groups/mine");
  $("groupsList").innerHTML = groups.map((g) => `
    <div class="item-card">
      <h4>${escapeHtml(g.name)} <span class="item-meta">· invite code: ${g.invite_code}</span></h4>
      <p>${g.members.map((m) => `${escapeHtml(m.name)} (${m.study_minutes}m)`).join(", ")}</p>
    </div>
  `).join("") || `<p class="hint">No groups yet.</p>`;
}
$("createGroupBtn").onclick = async () => {
  const name = $("groupName").value.trim();
  if (!name) return;
  const res = await api("/api/groups/create", { name });
  toast(`Group created — invite code: ${res.invite_code}`);
  $("groupName").value = "";
  loadGroups();
};
$("joinGroupBtn").onclick = async () => {
  const code = $("joinCode").value.trim();
  if (!code) return;
  try { await api("/api/groups/join", { invite_code: code }); $("joinCode").value = ""; loadGroups(); }
  catch (err) { toast(err.message); }
};

// ---------- Glory / Leaderboard ----------
async function loadLeaderboard() {
  const rows = await api("/api/leaderboard");
  $("leaderboardList").innerHTML = rows.map((r, i) => `
    <div class="item-card"><h4>#${i + 1} ${escapeHtml(r.name)}</h4><p>${r.study_minutes} minutes · ${r.xp} XP</p></div>
  `).join("") || `<p class="hint">No one on the board yet.</p>`;
}

// ---------- Sources ----------
async function loadSources() {
  const sources = await api("/api/sources");
  $("sourcesList").innerHTML = sources.map((s) => `
    <div class="item-card"><h4>${escapeHtml(s.title)} <span class="ai-tag">${s.origin || "manual"}</span></h4>
    <p>${escapeHtml((s.content || "").slice(0, 200))}${(s.content || "").length > 200 ? "…" : ""}</p></div>
  `).join("") || `<p class="hint">No sources yet.</p>`;
}
$("addSourceBtn").onclick = async () => {
  const title = $("sourceTitle").value.trim(), content = $("sourceContent").value.trim();
  if (!title || !content) return toast("Title and content are both needed.");
  await api("/api/sources", { title, content });
  $("sourceTitle").value = ""; $("sourceContent").value = "";
  loadSources();
};
$("uploadFile").addEventListener("change", () => {
  const f = $("uploadFile").files[0];
  $("uploadFileName").textContent = f ? f.name : "Choose PDF or image";
});
$("uploadBtn").onclick = async () => {
  const file = $("uploadFile").files[0];
  if (!file) return toast("Choose a PDF or image first.");
  const fd = new FormData(); fd.append("file", file);
  $("uploadBtn").textContent = "Reading..."; $("uploadBtn").disabled = true;
  try {
    await apiUpload("/api/upload", fd);
    toast("Source added from file.");
    $("uploadFile").value = "";
    $("uploadFileName").textContent = "Choose PDF or image";
    loadSources();
  } catch (err) { toast(err.message); }
  finally { $("uploadBtn").textContent = "Upload"; $("uploadBtn").disabled = false; }
};
$("youtubeBtn").onclick = async () => {
  const url = $("youtubeUrl").value.trim();
  if (!url) return toast("Paste a YouTube link first.");
  $("youtubeBtn").textContent = "Importing..."; $("youtubeBtn").disabled = true;
  try {
    await api("/api/sources/youtube", { url });
    toast("Transcript imported.");
    $("youtubeUrl").value = "";
    loadSources();
  } catch (err) { toast(err.message); }
  finally { $("youtubeBtn").textContent = "Import transcript"; $("youtubeBtn").disabled = false; }
};

async function fillSourceDropdown(selectId) {
  try {
    const sources = await api("/api/sources");
    const sel = $(selectId);
    const current = sel.value;
    sel.innerHTML = `<option value="">— no source —</option>` +
      sources.map((s) => `<option value="${s.id}">${escapeHtml(s.title)}</option>`).join("");
    sel.value = current;
  } catch (e) { /* ignore */ }
}

// ---------- Dopa / AI chat ----------
async function loadChat() {
  const history = await api("/api/ai/history");
  $("chatLog").innerHTML = history.map((m) => bubbleHtml(m.role, m.content)).join("");
  $("chatLog").scrollTop = $("chatLog").scrollHeight;
}
function bubbleHtml(role, content) {
  const cls = role === "user" ? "chat-user" : "chat-ai";
  const label = role === "user" ? "You" : "Dopamind";
  return `<div class="chat-bubble ${cls}"><div class="chat-label">${label}</div>${escapeHtml(content)}</div>`;
}
async function sendChat() {
  const msg = $("chatInput").value.trim();
  if (!msg) return;
  $("chatInput").value = "";
  $("chatLog").insertAdjacentHTML("beforeend", bubbleHtml("user", msg));
  const aiId = "ai-" + Date.now();
  $("chatLog").insertAdjacentHTML("beforeend", `<div class="chat-bubble chat-ai" id="${aiId}"><div class="chat-label">Dopamind</div><span></span></div>`);
  $("chatLog").scrollTop = $("chatLog").scrollHeight;
  const target = document.querySelector(`#${aiId} span`);
  try {
    const res = await fetch("/api/ai/chat", {
      method: "POST", credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: msg }),
    });
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let full = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      full += decoder.decode(value, { stream: true });
      target.textContent = full;
      $("chatLog").scrollTop = $("chatLog").scrollHeight;
    }
  } catch (err) { target.textContent = "Something went wrong: " + err.message; }
}
$("chatSendBtn").onclick = sendChat;
$("chatInput").addEventListener("keydown", (e) => { if (e.key === "Enter") sendChat(); });

// voice note recording
let mediaRecorder = null, audioChunks = [];
$("voiceNoteBtn").onclick = async () => {
  if (mediaRecorder && mediaRecorder.state === "recording") {
    mediaRecorder.stop();
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    mediaRecorder = new MediaRecorder(stream);
    audioChunks = [];
    mediaRecorder.ondataavailable = (e) => audioChunks.push(e.data);
    mediaRecorder.onstop = async () => {
      $("voiceNoteBtn").classList.remove("recording");
      const blob = new Blob(audioChunks, { type: "audio/webm" });
      const fd = new FormData(); fd.append("audio", blob, "voice.webm");
      toast("Transcribing voice note...");
      try {
        const res = await apiUpload("/api/voice-note", fd);
        toast(`Saved as note: "${res.title}"`);
      } catch (err) { toast(err.message); }
      stream.getTracks().forEach((t) => t.stop());
    };
    mediaRecorder.start();
    $("voiceNoteBtn").classList.add("recording");
    toast("Recording... click the mic again to stop.");
  } catch (err) { toast("Microphone access denied or unavailable."); }
};

// ---------- Account ----------
$("changePwBtn").onclick = async () => {
  const current_password = $("curPw").value, new_password = $("newPw").value, confirm_password = $("newPwConfirm").value;
  if (new_password !== confirm_password) return toast("New passwords don't match.");
  try {
    await api("/api/password/change", { current_password, new_password, confirm_password });
    toast("Password updated.");
    $("curPw").value = ""; $("newPw").value = ""; $("newPwConfirm").value = "";
  } catch (err) { toast(err.message); }
};
$("deleteAccBtn").onclick = async () => {
  if (!confirm("This permanently deletes your account and all your data. Continue?")) return;
  try {
    await api("/api/account/delete", { password: $("delPw").value });
    toast("Account deleted.");
    $("app").classList.add("hidden");
    $("authScreen").classList.remove("hidden");
    showAuthForm("loginForm");
  } catch (err) { toast(err.message); }
};

// ---------- utils ----------
function escapeHtml(str) {
  return (str || "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---------- boot ----------
(async function boot() {
  try {
    me = await api("/api/me");
    if (me.logged_in) { await enterApp(); return; }
  } catch (e) { /* not logged in */ }
  showAuthForm("loginForm");
})();
