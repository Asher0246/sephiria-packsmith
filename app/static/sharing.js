// Consent is kept by the local service so changing launch ports does not reset it.
let sharingChoice = null;

function displaySharing(status) {
  sharingChoice = status.enabled;
  $("sharingState").textContent = status.enabled
    ? `自动分享已开启${status.pending ? ` · ${status.pending} 条待上传` : ""}${status.uploadFailed ? " · 上传暂未成功，稍后自动重试" : ""}`
    : "自动分享未开启";
  $("sharingTitle").textContent = status.needsConsent ? "是否自动分享构筑？" : "设置 · 数据分享";
  $("sharingAccept").textContent = status.enabled ? "保持开启" : "同意并开启";
  $("sharingDecline").textContent = status.needsConsent ? "暂不开启" : "关闭自动分享";
  // The first-run question is a binary choice: "返回" would only repeat
  // "暂不开启", and there is no sharing state to report yet.
  $("sharingClose").hidden = status.needsConsent;
  $("sharingState").hidden = status.needsConsent;
}

async function saveSharing(enabled) {
  $("sharingAccept").disabled = $("sharingDecline").disabled = true;
  try {
    const status = await api("/api/sharing", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    displaySharing(status);
    $("sharingDialog").close();
    showToast(enabled ? "已开启自动分享构筑" : "已关闭自动分享，待上传记录已清除");
  } catch (error) { showToast(error.message); }
  finally { $("sharingAccept").disabled = $("sharingDecline").disabled = false; }
}

async function openSharingSettings() {
  try {
    displaySharing(await api("/api/sharing"));
    $("sharingDialog").showModal();
  } catch (error) { showToast(error.message); }
}

async function initSharing() {
  $("settingsBtn").addEventListener("click", openSharingSettings);
  $("sharingAccept").addEventListener("click", () => saveSharing(true));
  $("sharingDecline").addEventListener("click", () => saveSharing(false));
  $("sharingClose").addEventListener("click", () => $("sharingDialog").close());
  $("sharingDialog").addEventListener("close", async () => {
    // Dismissing the first-run question records a refusal, never consent.
    try {
      const status = await api("/api/sharing");
      if (status.needsConsent) await saveSharing(false);
    } catch (error) { showToast(error.message); }
  });
  try {
    const status = await api("/api/sharing");
    displaySharing(status);
    if (status.needsConsent) $("sharingDialog").showModal();
  } catch (error) { showToast(`无法读取数据分享设置：${error.message}`); }
}
initSharing();
