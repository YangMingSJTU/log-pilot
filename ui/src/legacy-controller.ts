// @ts-nocheck
import { apiFetch } from "./api";
import { chooseRepository } from "./desktop";

export function mountLegacyController(): void {const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[char]));
    const state = {
      path: "",
      scanning: false,
      scanJobId: "",
      scanReportVersion: -1,
      scanCancelRequested: false,
      browsing: false,
      history: [],
      report: null,
      reportActionable: false,
      patch: "",
      activeRunId: "",
      selectedGroups: new Set(),
      expandedGroups: new Set(),
      collapsedFiles: new Set(),
      searchQuery: "",
      severityFilter: "all",
      actionFilter: "all",
      resultModules: [],
      activeModule: "",
      issueOffset: 0,
      issueLimit: 100,
      issueTotal: 0,
      issueLoading: false,
      scanPlan: null,
      pendingExcludedMappings: [],
      planRuntime: null,
      planPath: "",
      appliedIssueIds: new Set(),
      applyRecords: [],
      latestApplyId: "",
      canRollback: false,
      pendingIssueIds: [],
      applying: false,
      diagnosticsOpen: false,
      runtimes: [],
      selectedRuntime: "",
      repositorySettings: {
        language_mode: "auto",
        selected_languages: [],
        templates: {},
        language_presets: [],
        template_presets: [],
        active_language_preset: "auto",
        active_template_preset: "auto",
        analysis_depth: "standard"
      },
      languageProfile: { detected_languages: [], template_recommendations: {} },
      settingsLanguages: [],
      templateLanguage: "python",
      settingsBusy: false,
      presetDialogType: "",
      activeView: "current"
    };
    const repoPath = document.querySelector("#repoPath");
    const browseButton = document.querySelector("#browseButton");
    const scanButton = document.querySelector("#scanButton");
    const scanProgress = document.querySelector("#scanProgress");
    const scanProgressTitle = document.querySelector("#scanProgressTitle");
    const scanProgressPercent = document.querySelector("#scanProgressPercent");
    const scanProgressMessage = document.querySelector("#scanProgressMessage");
    const scanProgressTrack = document.querySelector("#scanProgressTrack");
    const scanSteps = document.querySelector("#scanSteps");
    const scanModules = document.querySelector("#scanModules");
    const cancelScanButton = document.querySelector("#cancelScanButton");
    const incrementalNote = document.querySelector("#incrementalNote");
    const toastRegion = document.querySelector("#toastRegion");
    const currentTab = document.querySelector("#currentTab");
    const historyTab = document.querySelector("#historyTab");
    const settingsTab = document.querySelector("#settingsTab");
    const currentPanel = document.querySelector("#currentPanel");
    const historyPanel = document.querySelector("#historyPanel");
    const settingsPanel = document.querySelector("#settingsPanel");
    const runtimeSelect = document.querySelector("#runtimeSelect");
    const runtimeDot = document.querySelector("#runtimeDot");
    const refreshRuntimesButton = document.querySelector("#refreshRuntimesButton");
    const resultSearch = document.querySelector("#resultSearch");
    const resultModule = document.querySelector("#resultModule");
    const severityFilters = document.querySelector("#severityFilters");
    const actionFilters = document.querySelector("#actionFilters");
    const resultStream = document.querySelector("#resultStream");
    const resultsSummary = document.querySelector("#resultsSummary");
    const previousIssues = document.querySelector("#previousIssues");
    const nextIssues = document.querySelector("#nextIssues");
    const issuePageLabel = document.querySelector("#issuePageLabel");
    const expandAllButton = document.querySelector("#expandAllButton");
    const collapseAllButton = document.querySelector("#collapseAllButton");
    const fullPatchButton = document.querySelector("#fullPatchButton");
    const fullPatchDialog = document.querySelector("#fullPatchDialog");
    const fullPatchPre = document.querySelector("#fullPatchPre");
    const closePatchDialog = document.querySelector("#closePatchDialog");
    const diagnosticsToggle = document.querySelector("#diagnosticsToggle");
    const diagnosticsPre = document.querySelector("#diagnosticsPre");
    const batchBar = document.querySelector("#batchBar");
    const batchSelectionCount = document.querySelector("#batchSelectionCount");
    const batchSelectionFiles = document.querySelector("#batchSelectionFiles");
    const clearSelectionButton = document.querySelector("#clearSelectionButton");
    const batchApplyButton = document.querySelector("#batchApplyButton");
    const snapshotBanner = document.querySelector("#snapshotBanner");
    const coverageBanner = document.querySelector("#coverageBanner");
    const mappingBanner = document.querySelector("#mappingBanner");
    const rollbackButton = document.querySelector("#rollbackButton");
    const rescanButton = document.querySelector("#rescanButton");
    const applyDialog = document.querySelector("#applyDialog");
    const applySummary = document.querySelector("#applySummary");
    const closeApplyDialog = document.querySelector("#closeApplyDialog");
    const cancelApplyButton = document.querySelector("#cancelApplyButton");
    const confirmApplyButton = document.querySelector("#confirmApplyButton");
    const saveSettingsButton = document.querySelector("#saveSettingsButton");
    const profileRepositoryButton = document.querySelector("#profileRepositoryButton");
    const languageMode = document.querySelector("#languageMode");
    const languageOptions = document.querySelector("#languageOptions");
    const templateLanguageNav = document.querySelector("#templateLanguageNav");
    const templateInput = document.querySelector("#templateInput");
    const templateSource = document.querySelector("#templateSource");
    const templateSupport = document.querySelector("#templateSupport");
    const useRecommendedTemplate = document.querySelector("#useRecommendedTemplate");
    const useBuiltinTemplate = document.querySelector("#useBuiltinTemplate");
    const analysisLanguagePreset = document.querySelector("#analysisLanguagePreset");
    const analysisTemplatePreset = document.querySelector("#analysisTemplatePreset");
    const analysisDepth = document.querySelector("#analysisDepth");
    const analysisLanguageSummary = document.querySelector("#analysisLanguageSummary");
    const analysisTemplateSummary = document.querySelector("#analysisTemplateSummary");
    const analysisDepthSummary = document.querySelector("#analysisDepthSummary");
    const addLanguagePreset = document.querySelector("#addLanguagePreset");
    const addTemplatePreset = document.querySelector("#addTemplatePreset");
    const settingsLanguagePreset = document.querySelector("#settingsLanguagePreset");
    const settingsTemplatePreset = document.querySelector("#settingsTemplatePreset");
    const loadLanguagePreset = document.querySelector("#loadLanguagePreset");
    const loadTemplatePreset = document.querySelector("#loadTemplatePreset");
    const saveLanguagePreset = document.querySelector("#saveLanguagePreset");
    const saveTemplatePreset = document.querySelector("#saveTemplatePreset");
    const deleteLanguagePreset = document.querySelector("#deleteLanguagePreset");
    const deleteTemplatePreset = document.querySelector("#deleteTemplatePreset");
    const presetDialog = document.querySelector("#presetDialog");
    const presetDialogTitle = document.querySelector("#presetDialogTitle");
    const presetDialogDescription = document.querySelector("#presetDialogDescription");
    const presetNameInput = document.querySelector("#presetNameInput");
    const closePresetDialog = document.querySelector("#closePresetDialog");
    const cancelPresetButton = document.querySelector("#cancelPresetButton");
    const confirmPresetButton = document.querySelector("#confirmPresetButton");
    const planDialog = document.querySelector("#planDialog");
    const planSummary = document.querySelector("#planSummary");
    const planExclusions = document.querySelector("#planExclusions");
    const planModules = document.querySelector("#planModules");
    const planSelectionSummary = document.querySelector("#planSelectionSummary");
    const closePlanDialog = document.querySelector("#closePlanDialog");
    const cancelPlanButton = document.querySelector("#cancelPlanButton");
    const confirmPlanButton = document.querySelector("#confirmPlanButton");
    const selectAllModules = document.querySelector("#selectAllModules");
    const selectRecommendedModules = document.querySelector("#selectRecommendedModules");
    let issueSearchTimer = 0;

    scanButton.addEventListener("click", () => startScan(repoPath.value));
    cancelScanButton.addEventListener("click", cancelScan);
    closePlanDialog.addEventListener("click", closeScanPlan);
    cancelPlanButton.addEventListener("click", closeScanPlan);
    confirmPlanButton.addEventListener("click", confirmScanPlan);
    selectAllModules.addEventListener("click", () => setPlanSelection("all"));
    selectRecommendedModules.addEventListener("click", () => setPlanSelection("recommended"));
    planModules.addEventListener("change", updatePlanSelectionSummary);
    scanModules.addEventListener("click", event => {
      const button = event.target.closest("button[data-retry-module]");
      if (button) retryFailedModule(button.dataset.retryModule);
    });
    browseButton.addEventListener("click", browseRepository);
    repoPath.addEventListener("keydown", event => {
      if (event.key === "Enter") startScan(repoPath.value);
    });
    repoPath.addEventListener("change", () => activateRepository(repoPath.value));
    currentTab.addEventListener("click", () => showTab("current"));
    historyTab.addEventListener("click", () => showTab("history"));
    settingsTab.addEventListener("click", () => {
      showTab("settings");
      loadRepositorySettings(repoPath.value, true);
    });
    refreshRuntimesButton.addEventListener("click", () => loadRuntimes(true));
    resultModule.addEventListener("change", async () => {
      state.activeModule = resultModule.value;
      state.issueOffset = 0;
      await loadIssuePage();
    });
    previousIssues.addEventListener("click", async () => {
      state.issueOffset = Math.max(0, state.issueOffset - state.issueLimit);
      await loadIssuePage();
    });
    nextIssues.addEventListener("click", async () => {
      if (state.issueOffset + state.issueLimit >= state.issueTotal) return;
      state.issueOffset += state.issueLimit;
      await loadIssuePage();
    });
    runtimeSelect.addEventListener("change", () => {
      state.selectedRuntime = runtimeSelect.value;
      window.localStorage.setItem("logpilot.runtime", state.selectedRuntime);
      renderRuntimes();
    });
    analysisLanguagePreset.addEventListener("change", () => selectAnalysisPreset("language", analysisLanguagePreset.value));
    analysisTemplatePreset.addEventListener("change", () => selectAnalysisPreset("template", analysisTemplatePreset.value));
    analysisDepth.addEventListener("change", async () => {
      const previous = state.repositorySettings.analysis_depth || "standard";
      state.repositorySettings.analysis_depth = analysisDepth.value;
      renderAnalysisDepth();
      if (await persistRepositorySettings(true)) showToast(`AI 分析深度已设为${analysisDepth.options[analysisDepth.selectedIndex].text}`, "success");
      else {
        state.repositorySettings.analysis_depth = previous;
        renderAnalysisDepth();
      }
    });
    addLanguagePreset.addEventListener("click", () => openPresetDialog("language"));
    addTemplatePreset.addEventListener("click", () => openPresetDialog("template"));
    loadLanguagePreset.addEventListener("click", () => loadSavedPreset("language", settingsLanguagePreset.value));
    loadTemplatePreset.addEventListener("click", () => loadSavedPreset("template", settingsTemplatePreset.value));
    saveLanguagePreset.addEventListener("click", () => openPresetDialog("language"));
    saveTemplatePreset.addEventListener("click", () => openPresetDialog("template"));
    deleteLanguagePreset.addEventListener("click", () => deleteSavedPreset("language", settingsLanguagePreset.value));
    deleteTemplatePreset.addEventListener("click", () => deleteSavedPreset("template", settingsTemplatePreset.value));
    settingsLanguagePreset.addEventListener("change", updatePresetLibraryActions);
    settingsTemplatePreset.addEventListener("change", updatePresetLibraryActions);
    closePresetDialog.addEventListener("click", closePresetEditor);
    cancelPresetButton.addEventListener("click", closePresetEditor);
    confirmPresetButton.addEventListener("click", createPreset);
    presetNameInput.addEventListener("keydown", event => {
      if (event.key === "Enter") createPreset();
    });
    presetDialog.addEventListener("click", event => {
      if (event.target === presetDialog) closePresetEditor();
    });
    planDialog.addEventListener("click", event => {
      if (event.target === planDialog) closeScanPlan();
    });
    resultSearch.addEventListener("input", () => {
      state.searchQuery = resultSearch.value;
      window.clearTimeout(issueSearchTimer);
      issueSearchTimer = window.setTimeout(() => {
        state.issueOffset = 0;
        loadIssuePage();
      }, 220);
    });
    severityFilters.addEventListener("click", event => {
      const button = event.target.closest("button[data-severity]");
      if (!button) return;
      state.severityFilter = button.dataset.severity;
      state.issueOffset = 0;
      loadIssuePage();
    });
    actionFilters.addEventListener("click", event => {
      const button = event.target.closest("button[data-action]");
      if (!button) return;
      state.actionFilter = button.dataset.action;
      state.issueOffset = 0;
      loadIssuePage();
    });
    resultStream.addEventListener("click", handleResultStreamClick);
    resultStream.addEventListener("change", handleResultStreamChange);
    expandAllButton.addEventListener("click", () => setVisibleGroupsExpanded(true));
    collapseAllButton.addEventListener("click", () => setVisibleGroupsExpanded(false));
    fullPatchButton.addEventListener("click", openFullPatch);
    closePatchDialog.addEventListener("click", closeFullPatch);
    fullPatchDialog.addEventListener("click", event => {
      if (event.target === fullPatchDialog) closeFullPatch();
    });
    diagnosticsToggle.addEventListener("click", toggleDiagnostics);
    batchApplyButton.addEventListener("click", () => {
      const groups = issueGroups().filter(group => state.selectedGroups.has(group.id));
      openApplyDialog(groups.flatMap(group => patchIssueIds(group)));
    });
    clearSelectionButton.addEventListener("click", () => {
      state.selectedGroups = new Set();
      renderResultStream();
    });
    rollbackButton.addEventListener("click", rollbackLatestApply);
    rescanButton.addEventListener("click", () => startScan(repoPath.value));
    closeApplyDialog.addEventListener("click", closeApplyConfirmation);
    cancelApplyButton.addEventListener("click", closeApplyConfirmation);
    confirmApplyButton.addEventListener("click", submitApply);
    saveSettingsButton.addEventListener("click", saveRepositorySettings);
    profileRepositoryButton.addEventListener("click", profileRepository);
    languageMode.addEventListener("click", event => {
      const button = event.target.closest("button[data-language-mode]");
      if (!button) return;
      state.repositorySettings.language_mode = button.dataset.languageMode;
      state.repositorySettings.active_language_preset = "auto";
      if (button.dataset.languageMode === "custom" && !state.repositorySettings.selected_languages.length) {
        const detected = state.languageProfile.detected_languages.filter(item => item.recommended).map(item => item.id);
        state.repositorySettings.selected_languages = detected.length ? detected : ["python"];
      }
      renderRepositorySettings();
    });
    languageOptions.addEventListener("change", event => {
      const input = event.target.closest("input[data-language-id]");
      if (!input) return;
      const selected = new Set(state.repositorySettings.selected_languages);
      if (input.checked) selected.add(input.dataset.languageId);
      else selected.delete(input.dataset.languageId);
      state.repositorySettings.selected_languages = [...selected];
      state.repositorySettings.active_language_preset = "auto";
      renderRepositorySettings();
    });
    templateLanguageNav.addEventListener("click", event => {
      const button = event.target.closest("button[data-template-language]");
      if (!button) return;
      state.templateLanguage = button.dataset.templateLanguage;
      renderRepositorySettings();
    });
    templateInput.addEventListener("input", () => {
      state.repositorySettings.templates[state.templateLanguage] = templateInput.value;
      state.repositorySettings.active_template_preset = "auto";
      renderTemplateMeta();
    });
    useRecommendedTemplate.addEventListener("click", () => {
      const recommendation = templateRecommendation(state.templateLanguage);
      state.repositorySettings.templates[state.templateLanguage] = recommendation.template || languageDefinition(state.templateLanguage)?.builtin_template || "";
      state.repositorySettings.active_template_preset = "auto";
      renderRepositorySettings();
    });
    useBuiltinTemplate.addEventListener("click", () => {
      state.repositorySettings.templates[state.templateLanguage] = languageDefinition(state.templateLanguage)?.builtin_template || "";
      state.repositorySettings.active_template_preset = "auto";
      renderRepositorySettings();
    });
    applyDialog.addEventListener("click", event => {
      if (event.target === applyDialog) closeApplyConfirmation();
    });
    document.addEventListener("keydown", event => {
      if (event.key !== "Escape") return;
      if (!planDialog.classList.contains("hidden")) closeScanPlan();
      else if (!applyDialog.classList.contains("hidden")) closeApplyConfirmation();
      else if (!fullPatchDialog.classList.contains("hidden")) closeFullPatch();
      else if (!presetDialog.classList.contains("hidden")) closePresetEditor();
    });

    async function init() {
      try {
        const [stateResponse] = await Promise.all([apiFetch("/api/state"), loadRuntimes(false)]);
        const payload = await stateResponse.json();
        if (!stateResponse.ok || payload.error) throw new Error(payload.error || "状态读取失败");
        const activeScan = payload.active_scan || null;
        state.path = activeScan?.repository || payload.repository || "";
        state.history = payload.history || [];
        state.activeRunId = state.history[0]?.run_id || "";
        repoPath.value = state.path;
        updateRepositoryIdentity(state.path);
        await loadRepositorySettings(state.path, true);
        renderHistory(state.history);
        if (activeScan) {
          renderEmpty();
          await resumeScan(activeScan);
        } else if (payload.has_report) await loadReport();
        else renderEmpty();
      } catch (error) {
        showToast(await requestFailureMessage(error, "初始化失败"), "error");
        renderEmpty();
      }
    }

    async function loadRuntimes(refresh) {
      refreshRuntimesButton.disabled = true;
      try {
        const response = await apiFetch(refresh ? "/api/runtimes/refresh" : "/api/runtimes", {
          method: refresh ? "POST" : "GET"
        });
        const payload = await response.json();
        if (!response.ok || payload.error) throw new Error(payload.error || "运行时检测失败");
        state.runtimes = payload.runtimes || [];
        const online = state.runtimes.filter(runtime => runtime.status === "online");
        const remembered = window.localStorage.getItem("logpilot.runtime");
        const preferred = [remembered, payload.selected, "codex", "claude"].find(id =>
          online.some(runtime => runtime.id === id)
        );
        state.selectedRuntime = preferred || "";
        renderRuntimes();
        if (refresh) showToast(`运行时状态已刷新，${online.length} 个在线`, "success");
      } catch (error) {
        state.runtimes = [];
        state.selectedRuntime = "";
        renderRuntimes();
        if (refresh) showToast(await requestFailureMessage(error, "刷新失败"), "error");
      } finally {
        refreshRuntimesButton.disabled = false;
      }
    }

    async function activateRepository(path, quiet = false) {
      const target = String(path || "").trim();
      if (!target) {
        showToast("请先输入或选择本地仓库路径", "warning");
        return false;
      }
      try {
        const response = await apiFetch("/api/repository", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: target })
        });
        const payload = await response.json();
        if (!response.ok || payload.error) throw new Error(payload.error || "仓库切换失败");
        await applyRepositoryState(payload);
        if (!quiet) showToast("仓库路径已记住", "success");
        return true;
      } catch (error) {
        repoPath.value = state.path;
        showToast(await requestFailureMessage(error, "仓库切换失败"), "error");
        return false;
      }
    }

    async function applyRepositoryState(payload) {
      state.path = payload.repository || payload.path || state.path;
      state.history = payload.history || [];
      state.activeRunId = state.history[0]?.run_id || "";
      repoPath.value = state.path;
      updateRepositoryIdentity(state.path);
      await loadRepositorySettings(state.path, true);
      renderHistory(state.history);
      if (payload.has_report) await loadReport();
      else renderEmpty();
    }

    async function loadRepositorySettings(path, quiet = false) {
      const target = String(path || "").trim();
      if (!target || state.settingsBusy) return;
      state.settingsBusy = true;
      updateSettingsBusy();
      try {
        const response = await apiFetch(`/api/settings?path=${encodeURIComponent(target)}`);
        const payload = await response.json();
        if (!response.ok || payload.error) throw new Error(payload.error || "仓库设置读取失败");
        state.repositorySettings = {
          ...emptyRepositorySettings(),
          ...(payload.settings || {}),
          templates: { ...(payload.settings?.templates || {}) },
          language_presets: [...(payload.settings?.language_presets || [])],
          template_presets: [...(payload.settings?.template_presets || [])],
          language_mode: "auto",
          selected_languages: [],
          active_language_preset: "auto"
        };
        state.languageProfile = payload.profile || { detected_languages: [], template_recommendations: {} };
        state.settingsLanguages = payload.languages || [];
        if (!state.settingsLanguages.some(item => item.id === state.templateLanguage)) {
          state.templateLanguage = state.settingsLanguages[0]?.id || "python";
        }
        renderRepositorySettings(payload.repository || target);
      } catch (error) {
        if (!quiet) showToast(await requestFailureMessage(error, "设置读取失败"), "error");
      } finally {
        state.settingsBusy = false;
        updateSettingsBusy();
      }
    }

    async function saveRepositorySettings() {
      await persistRepositorySettings(false);
    }

    async function persistRepositorySettings(quiet = false) {
      if (state.settingsBusy) {
        if (!quiet) showToast("设置正在处理中，请稍候", "warning");
        return false;
      }
      const target = repoPath.value.trim();
      if (!target) {
        showToast("请先输入或选择本地仓库路径", "warning");
        return false;
      }
      state.repositorySettings.language_mode = "auto";
      state.repositorySettings.selected_languages = [];
      state.repositorySettings.active_language_preset = "auto";
      state.settingsBusy = true;
      updateSettingsBusy();
      try {
        const response = await apiFetch("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: target, settings: state.repositorySettings })
        });
        const payload = await response.json();
        if (!response.ok || payload.error) throw new Error(payload.error || "设置保存失败");
        state.repositorySettings = payload.settings;
        state.languageProfile = payload.profile || state.languageProfile;
        state.settingsLanguages = payload.languages || state.settingsLanguages;
        renderRepositorySettings(payload.repository);
        if (!quiet) showToast("仓库设置已保存", "success");
        return true;
      } catch (error) {
        showToast(await requestFailureMessage(error, "保存失败"), "error");
        return false;
      } finally {
        state.settingsBusy = false;
        updateSettingsBusy();
      }
    }

    async function profileRepository() {
      if (state.settingsBusy) return;
      state.settingsBusy = true;
      updateSettingsBusy();
      showToast("正在识别语言和日志风格...", "info", 0);
      try {
        const response = await apiFetch("/api/settings/profile", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: repoPath.value.trim() })
        });
        const payload = await response.json();
        if (!response.ok || payload.error) throw new Error(payload.error || "仓库识别失败");
        state.languageProfile = payload.profile || state.languageProfile;
        state.settingsLanguages = payload.languages || state.settingsLanguages;
        renderRepositorySettings(payload.repository);
        showToast("语言与日志模板推荐已更新", "success");
      } catch (error) {
        showToast(await requestFailureMessage(error, "识别失败"), "error");
      } finally {
        state.settingsBusy = false;
        updateSettingsBusy();
      }
    }

    async function browseRepository() {
      if (state.browsing) return;
      setBrowsing(true);
      showToast("正在打开仓库选择器...", "info", 0);
      try {
        const desktopPath = await chooseRepository(repoPath.value.trim());
        if (desktopPath === undefined) {
          showToast("浏览器调试模式请直接输入仓库路径。", "info");
          return;
        }
        if (desktopPath === null) {
          showToast("已取消选择", "info");
          return;
        }
        const response = await apiFetch("/api/repository", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: desktopPath })
        });
        const payload = await response.json();
        if (!response.ok || payload.error) throw new Error(payload.error || "仓库切换失败");
        await applyRepositoryState(payload);
        showToast("仓库路径已更新并记住", "success");
      } catch (error) {
        showToast(await requestFailureMessage(error, "选择失败"), "error");
      } finally {
        setBrowsing(false);
      }
    }

    async function startScan(path) {
      if (state.scanning) return;
      const target = path.trim();
      if (!target) {
        showToast("请先输入或选择本地仓库路径", "warning");
        return;
      }
      if (target !== state.path && !await activateRepository(target, true)) return;
      if (!await persistRepositorySettings(true)) return;
      scanButton.disabled = true;
      showToast("正在预检仓库并规划目录...", "info", 0);
      try {
        const planResponse = await apiFetch("/api/scan/plans", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: target })
        });
        const planPayload = await planResponse.json();
        if (!planResponse.ok || planPayload.error) throw new Error(planPayload.error || "仓库预检失败");
        const plan = planPayload.plan;
        if (!Number(plan.source_files || 0) || !plan.modules.length) {
          renderNoSourcePlan(plan);
          const message = Number(plan.source_files || 0)
            ? "发现了源码候选，但没有可分析文件"
            : "当前目录未发现源码文件";
          showToast(message, "warning");
          return;
        }
        const runtime = selectedRuntime();
        if (!runtime) throw new Error("没有可用运行时，请先在运行时页面检查 Codex 或 Claude");
        if ((plan.large_repository && plan.modules.length > 1) || (plan.excluded_mappings || []).length) {
          openScanPlan(plan, target, runtime);
          scanButton.disabled = false;
          return;
        }
        await submitPlannedScan(target, runtime, plan, plan.modules.map(module => module.id));
      } catch (error) {
        const message = await requestFailureMessage(error, "分析失败");
        showToast(message, "error");
        setScanning(false);
        markScanProgressFailed(message);
      } finally {
        if (!state.scanning) scanButton.disabled = false;
      }
    }

    async function submitPlannedScan(target, runtime, plan, moduleIds) {
      closeScanPlan();
      resetReportForScan(plan.excluded_mappings || []);
      setScanning(true);
      showToast(`已通过 ${runtime.name} 启动后台分析`, "info");
      try {
        const response = await apiFetch("/api/scans", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            path: target,
            runtime: runtime.id,
            plan_id: plan.id,
            module_ids: moduleIds
          })
        });
        const payload = await response.json();
        if (!response.ok && !(response.status === 409 && payload.job)) {
          throw new Error(payload.error || "分析任务创建失败");
        }
        const job = payload.job;
        if (!job?.job_id) throw new Error("分析任务缺少任务标识");
        state.scanJobId = job.job_id;
        state.scanReportVersion = -1;
        renderScanProgress(job);
        if (response.status === 409) showToast("已恢复该仓库正在执行的分析任务", "info");
        await pollScanJob(runtime);
      } catch (error) {
        const message = await requestFailureMessage(error, "分析失败");
        showToast(message, "error");
        setScanning(false);
        markScanProgressFailed(message);
      }
    }

    function openScanPlan(plan, path, runtime) {
      state.scanPlan = plan;
      state.planPath = path;
      state.planRuntime = runtime;
      planSummary.innerHTML = `
        <div class="plan-stat"><span>可分析文件</span><strong>${esc(plan.selected_files)}</strong></div>
        <div class="plan-stat"><span>仓库体积</span><strong>${esc(formatBytes(plan.total_bytes))}</strong></div>
        <div class="plan-stat"><span>目录模块</span><strong>${esc(plan.modules.length)}</strong></div>
        <div class="plan-stat"><span>已排除映射</span><strong>${esc((plan.excluded_mappings || []).length)}</strong></div>`;
      renderPlanExclusions(plan.excluded_mappings || []);
      planModules.innerHTML = plan.modules.map(module => `
        <label class="plan-module">
          <input type="checkbox" data-plan-module="${esc(module.id)}" ${module.recommended ? "checked" : ""}>
          <span><strong>${esc(module.path === "." ? "仓库根目录" : module.path)}</strong><span>${esc(Object.entries(module.languages || {}).map(([language, count]) => `${language} ${count}`).join(" · "))}</span></span>
          <em>${esc(module.file_count)} 文件 · ${esc(formatBytes(module.total_bytes))}</em>
        </label>`).join("");
      updatePlanSelectionSummary();
      planDialog.classList.remove("hidden");
    }

    function closeScanPlan() {
      planDialog.classList.add("hidden");
    }

    function renderPlanExclusions(mappings) {
      if (!mappings.length) {
        planExclusions.classList.add("hidden");
        planExclusions.innerHTML = "";
        return;
      }
      const visible = mappings.slice(0, 4).map(mapping => `
        <span><code>${esc(mapping.path)}</code> &rarr; <code>${esc(mapping.target)}</code></span>`).join("");
      const remaining = mappings.length > 4 ? `<span>其余 ${mappings.length - 4} 个映射目录将在结果中列出。</span>` : "";
      planExclusions.classList.remove("hidden");
      planExclusions.innerHTML = `<strong>检测到 ${mappings.length} 个目录映射，分析时将排除</strong><div class="mapping-list">${visible}${remaining}</div>`;
    }

    function setPlanSelection(mode) {
      const recommended = new Set((state.scanPlan?.modules || []).filter(item => item.recommended).map(item => item.id));
      planModules.querySelectorAll("input[data-plan-module]").forEach(input => {
        input.checked = mode === "all" || recommended.has(input.dataset.planModule);
      });
      updatePlanSelectionSummary();
    }

    function updatePlanSelectionSummary() {
      const selected = [...planModules.querySelectorAll("input[data-plan-module]:checked")];
      const modules = state.scanPlan?.modules || [];
      const selectedIds = new Set(selected.map(input => input.dataset.planModule));
      const files = modules.filter(module => selectedIds.has(module.id)).reduce((sum, module) => sum + module.file_count, 0);
      planSelectionSummary.textContent = `已选 ${selected.length} 个目录 · ${files} 个文件`;
      confirmPlanButton.disabled = selected.length === 0;
    }

    async function confirmScanPlan() {
      const moduleIds = [...planModules.querySelectorAll("input[data-plan-module]:checked")].map(input => input.dataset.planModule);
      if (!moduleIds.length || !state.scanPlan || !state.planRuntime) return;
      await submitPlannedScan(state.planPath, state.planRuntime, state.scanPlan, moduleIds);
    }

    function formatBytes(value) {
      const size = Number(value || 0);
      if (size >= 1024 ** 3) return `${(size / 1024 ** 3).toFixed(1)} GiB`;
      if (size >= 1024 ** 2) return `${(size / 1024 ** 2).toFixed(1)} MiB`;
      if (size >= 1024) return `${(size / 1024).toFixed(1)} KiB`;
      return `${size} B`;
    }

    async function resumeScan(job) {
      const runtime = state.runtimes.find(item => item.id === job.runtime_id) || selectedRuntime();
      if (!runtime) {
        showToast("检测到未完成分析，但对应运行时当前不可用", "warning");
        return;
      }
      state.selectedRuntime = runtime.id;
      renderRuntimes();
      resetReportForScan();
      state.scanJobId = job.job_id;
      state.scanReportVersion = -1;
      setScanning(true);
      if (job.partial_report) {
        state.scanReportVersion = job.report_version;
        renderPartialSummary(job.partial_report.summary);
      }
      renderScanProgress(job);
      showTab("current");
      showToast("已恢复正在进行的分析任务", "info");
      try {
        await pollScanJob(runtime);
      } catch (error) {
        setScanning(false);
        const message = await requestFailureMessage(error, "分析失败");
        markScanProgressFailed(message);
        showToast(message, "error");
      }
    }

    async function pollScanJob(runtime) {
      while (state.scanning && state.scanJobId) {
        const response = await apiFetch(
          `/api/scans/${encodeURIComponent(state.scanJobId)}?report_version=${state.scanReportVersion}`
        );
        const payload = await response.json();
        if (!response.ok || payload.error) throw new Error(payload.error || "分析状态读取失败");
        const job = payload.job;
        renderScanProgress(job);
        updateResultModules(job.modules || []);
        if (job.partial_report) {
          state.scanReportVersion = job.report_version;
          renderPartialSummary(job.partial_report.summary);
          if (job.run_id) await loadLiveIssues(job.run_id);
        }
        if (job.status === "completed") {
          await completeScan(job, runtime);
          return;
        }
        if (job.status === "failed") throw new Error(job.error || "后台分析失败");
        if (job.status === "cancelled") {
          setScanning(false);
          incrementalNote.classList.add("hidden");
          showToast("分析已取消，部分结果没有写入历史记录", "warning");
          return;
        }
        await new Promise(resolve => window.setTimeout(resolve, 650));
      }
    }

    async function completeScan(job, runtime) {
      state.path = job.repository;
      repoPath.value = state.path;
      updateRepositoryIdentity(state.path);
      const historyResponse = await apiFetch("/api/history");
      const historyPayload = await historyResponse.json();
      if (!historyResponse.ok || historyPayload.error) throw new Error(historyPayload.error || "历史记录刷新失败");
      state.history = historyPayload.runs || [];
      state.activeRunId = job.run_id || state.history[0]?.run_id || "";
      renderHistory(state.history);
      await loadReport(state.activeRunId);
      await loadRepositorySettings(state.path, true);
      showTab("current");
      setScanning(false);
      renderResultStream();
      renderScanProgress(job);
      incrementalNote.classList.add("hidden");
      showToast(`${runtime.name} 分析完成，结果已更新`, "success");
      window.setTimeout(() => {
        const hasFailedModule = (job.modules || []).some(module => module.selected && module.status === "failed");
        if (!state.scanning && !hasFailedModule) scanProgress.classList.add("hidden");
      }, 1800);
    }

    async function cancelScan() {
      if (!state.scanning || !state.scanJobId || state.scanCancelRequested) return;
      state.scanCancelRequested = true;
      cancelScanButton.disabled = true;
      cancelScanButton.textContent = "正在停止...";
      try {
        const response = await apiFetch(`/api/scans/${encodeURIComponent(state.scanJobId)}/cancel`, {
          method: "POST"
        });
        const payload = await response.json();
        if (!response.ok && response.status !== 409) throw new Error(payload.error || "停止失败");
        if (payload.job) renderScanProgress(payload.job);
      } catch (error) {
        state.scanCancelRequested = false;
        cancelScanButton.disabled = false;
        cancelScanButton.textContent = "停止分析";
        showToast(await requestFailureMessage(error, "停止失败"), "error");
      }
    }

    function resetReportForScan(excludedMappings = []) {
      state.report = null;
      state.reportActionable = false;
      state.patch = "";
      state.activeRunId = "";
      state.scanJobId = "";
      state.scanReportVersion = -1;
      state.scanCancelRequested = false;
      state.pendingExcludedMappings = excludedMappings;
      state.selectedGroups = new Set();
      state.expandedGroups = new Set();
      state.collapsedFiles = new Set();
      state.searchQuery = "";
      state.severityFilter = "all";
      state.actionFilter = "all";
      state.resultModules = [];
      state.activeModule = "";
      state.issueOffset = 0;
      state.issueTotal = 0;
      state.appliedIssueIds = new Set();
      state.applyRecords = [];
      resultSearch.value = "";
      resultModule.innerHTML = '<option value="">全部目录</option>';
      renderIssuePager();
      document.querySelector("#metrics").innerHTML = summaryMarkup(null);
      resultsSummary.textContent = "正在准备分析";
      resultStream.innerHTML = '<div class="results-empty">本地规则完成后将在这里显示第一批结果</div>';
      fullPatchButton.disabled = true;
      expandAllButton.disabled = true;
      collapseAllButton.disabled = true;
      batchBar.classList.add("hidden");
      snapshotBanner.classList.add("hidden");
      renderMappingBanner(excludedMappings);
      coverageBanner.classList.add("hidden");
      scanProgress.classList.remove("hidden", "failed", "completed");
      incrementalNote.classList.add("hidden");
      renderScanProgress({ status: "queued", stage: "queued", percent: 0, message: "正在创建后台分析任务", completed: 0, total: 0 });
    }

    function renderScanProgress(job) {
      const stageIndexes = { queued: 0, preparing: 0, discovering: 0, parsing: 1, framework: 2, rules: 2, runtime: 3, ai_missing: 3, fixes: 4, reporting: 4, complete: 5 };
      const titles = {
        queued: "等待开始",
        preparing: "准备分析",
        discovering: "发现源码文件",
        parsing: "解析源码",
        framework: "识别日志框架",
        rules: "执行本地规则",
        runtime: "运行时分析",
        ai_missing: "检查日志缺口",
        fixes: "生成修改建议",
        reporting: "保存分析报告",
        complete: "分析完成"
      };
      const terminal = ["completed", "failed", "cancelled"].includes(job.status);
      const currentIndex = stageIndexes[job.stage] ?? 0;
      const percent = Number(job.percent || 0);
      scanProgress.classList.remove("hidden");
      scanProgress.classList.toggle("failed", job.status === "failed");
      scanProgress.classList.toggle("completed", job.status === "completed");
      scanProgressTitle.textContent = job.status === "failed" ? "分析失败" : job.status === "cancelled" ? "分析已取消" : titles[job.stage] || "正在分析";
      scanProgressPercent.textContent = `${percent}%`;
      scanProgressMessage.textContent = job.message || "正在处理";
      scanProgressTrack.style.setProperty("--progress", `${percent}%`);
      scanProgressTrack.classList.toggle("indeterminate", !terminal && Number(job.total || 0) === 0);
      scanSteps.querySelectorAll("[data-scan-step]").forEach((step, index) => {
        step.classList.toggle("done", job.status === "completed" || index < currentIndex);
        step.classList.toggle("active", !terminal && index === currentIndex);
      });
      renderModuleProgress(job.modules || []);
      cancelScanButton.disabled = terminal || job.status === "cancelling" || state.scanCancelRequested;
      cancelScanButton.textContent = job.status === "cancelling" || state.scanCancelRequested ? "正在停止..." : "停止分析";
      incrementalNote.classList.toggle("hidden", !state.scanning || !state.report);
    }

    function markScanProgressFailed(message) {
      renderScanProgress({ status: "failed", stage: "failed", percent: 0, message, completed: 0, total: 1 });
      incrementalNote.classList.toggle("hidden", !state.report);
    }

    function renderModuleProgress(modules) {
      const visible = modules.filter(module => module.selected);
      scanModules.classList.toggle("hidden", !visible.length);
      scanModules.innerHTML = visible.map(module => {
        const action = module.status === "failed"
          ? `<button type="button" data-retry-module="${esc(module.id)}" ${state.scanning ? "disabled" : ""}>重试</button>`
          : `<span>${esc(module.completed_chunks)} / ${esc(module.total_chunks)}</span>`;
        return `<div class="module-progress-item ${esc(module.status)}"><i></i><strong title="${esc(module.path)}">${esc(module.path === "." ? "仓库根目录" : module.path)}</strong>${action}</div>`;
      }).join("");
    }

    async function retryFailedModule(moduleId) {
      if (!moduleId || state.scanning || !state.activeRunId) return;
      const runtime = selectedRuntime();
      if (!runtime) {
        showToast("没有可用运行时，无法重试目录", "warning");
        return;
      }
      setScanning(true);
      try {
        const response = await apiFetch(`/api/runs/${encodeURIComponent(state.activeRunId)}/modules/${encodeURIComponent(moduleId)}/retry`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ runtime: runtime.id })
        });
        const payload = await response.json();
        if (!response.ok || payload.error) throw new Error(payload.error || "目录重试失败");
        state.scanJobId = payload.job.job_id;
        state.scanReportVersion = -1;
        renderScanProgress(payload.job);
        showToast("正在重新分析失败目录", "info");
        await pollScanJob(runtime);
      } catch (error) {
        setScanning(false);
        showToast(await requestFailureMessage(error, "目录重试失败"), "error");
      }
    }

    function updateResultModules(modules) {
      const selected = modules.filter(module => module.selected);
      const signature = selected.map(module => `${module.id}:${module.issue_count}:${module.status}`).join("|");
      const currentSignature = state.resultModules.map(module => `${module.id}:${module.issue_count}:${module.status}`).join("|");
      if (signature === currentSignature) return;
      state.resultModules = selected;
      resultModule.innerHTML = '<option value="">全部目录</option>' + selected.map(module =>
        `<option value="${esc(module.id)}">${esc(module.path === "." ? "仓库根目录" : module.path)} · ${esc(module.issue_count)} 项</option>`
      ).join("");
      resultModule.value = state.activeModule;
    }

    function renderPartialSummary(partial) {
      if (!partial) return;
      const current = state.report?.summary || {};
      const summary = {
        ...current,
        files_scanned: partial.files_scanned ?? current.files_scanned ?? 0,
        discovered_files: current.discovered_files ?? partial.files_scanned ?? 0,
        log_count: partial.log_count ?? current.log_count ?? 0,
        issue_count: partial.issue_count ?? current.issue_count ?? 0,
        severity_counts: partial.severity_counts || current.severity_counts || {},
        score: null,
        score_status: "ai_incomplete",
        ai_status: "running"
      };
      if (!state.report) state.report = {
        summary,
        logs: [],
        issues: [],
        parse_failures: [],
        excluded_mappings: state.pendingExcludedMappings,
        language_insights: [],
        ai_traces: []
      };
      else state.report.summary = summary;
      renderMetrics(summary);
    }

    async function loadLiveIssues(runId) {
      if (!state.report) return;
      state.activeRunId = runId;
      try {
        await loadIssuePage(true);
      } catch (_error) {
        // The first transaction may not be committed yet.
      }
    }

    async function loadReport(runId = "") {
      const resolvedRunId = runId || state.history[0]?.run_id || state.activeRunId;
      if (!resolvedRunId) throw new Error("没有可读取的分析记录");
      const detailResponse = await apiFetch(`/api/runs/${encodeURIComponent(resolvedRunId)}`);
      const detail = await detailResponse.json();
      if (!detailResponse.ok || detail.error) throw new Error(detail.error || "分析记录读取失败");
      state.activeRunId = resolvedRunId;
      state.resultModules = [];
      state.activeModule = "";
      state.issueOffset = 0;
      state.report = {
        summary: detail.summary || {},
        logs: [],
        issues: [],
        parse_failures: detail.parse_failures || [],
        excluded_mappings: detail.excluded_mappings || [],
        language_insights: detail.language_insights || [],
        ai_traces: []
      };
      state.pendingExcludedMappings = state.report.excluded_mappings;
      updateResultModules(detail.modules || []);
      renderReport(state.report);
      state.reportActionable = true;
      if (detail.legacy) {
        const legacyResponse = await apiFetch(`/api/history/run?run_id=${encodeURIComponent(resolvedRunId)}`);
        const legacy = await legacyResponse.json();
        if (!legacyResponse.ok || legacy.error) throw new Error(legacy.error || "旧分析记录读取失败");
        state.report = legacy.report;
        renderReport(state.report);
      } else {
        await loadIssuePage();
      }
      await loadPatch();
      await loadApplies();
    }

    async function loadIssuePage(quiet = false) {
      if (!state.activeRunId || state.issueLoading) return;
      state.issueLoading = true;
      if (!quiet) resultStream.innerHTML = '<div class="results-empty">正在读取当前页...</div>';
      try {
        const params = new URLSearchParams({
          limit: String(state.issueLimit),
          offset: String(state.issueOffset)
        });
        if (state.activeModule) params.set("module", state.activeModule);
        if (state.severityFilter !== "all") params.set("severity", state.severityFilter);
        if (state.actionFilter !== "all") params.set("action", state.actionFilter);
        if (state.searchQuery.trim()) params.set("search", state.searchQuery.trim());
        const response = await apiFetch(`/api/runs/${encodeURIComponent(state.activeRunId)}/issues?${params}`);
        const payload = await response.json();
        if (!response.ok || payload.error) throw new Error(payload.error || "问题列表读取失败");
        state.issueTotal = Number(payload.total || 0);
        state.report.issues = payload.items || [];
        state.report.logs = Object.values(payload.logs || {});
        state.selectedGroups = new Set();
        state.expandedGroups = new Set(issueGroups().map(group => group.id));
        renderResultStream();
        renderIssuePager();
      } catch (error) {
        if (!quiet) showToast(await requestFailureMessage(error, "问题列表读取失败"), "error");
        throw error;
      } finally {
        state.issueLoading = false;
      }
    }

    function renderIssuePager() {
      const start = state.issueTotal ? state.issueOffset + 1 : 0;
      const end = Math.min(state.issueOffset + state.issueLimit, state.issueTotal);
      issuePageLabel.textContent = state.issueTotal ? `${start}-${end} / ${state.issueTotal}` : "0 项";
      previousIssues.disabled = state.issueOffset <= 0;
      nextIssues.disabled = state.issueOffset + state.issueLimit >= state.issueTotal;
    }

    async function loadPatch() {
      const suffix = state.activeRunId ? `?run_id=${encodeURIComponent(state.activeRunId)}` : "";
      const response = await apiFetch(`/api/patch${suffix}`);
      const text = await response.text();
      state.patch = response.ok ? text : "暂无补丁产物。";
      fullPatchButton.disabled = false;
      renderFullPatch(state.patch);
    }

    async function loadHistoryRun(runId) {
      showToast("正在读取历史分析...", "info", 0);
      try {
        await loadReport(runId);
        showTab("current");
        showToast("历史分析已加载", "success");
      } catch (error) {
        showToast(await requestFailureMessage(error, "历史记录读取失败"), "error");
      }
    }

    async function loadApplies() {
      if (!state.activeRunId) {
        setApplyState({});
        return;
      }
      const response = await apiFetch(`/api/applies?run_id=${encodeURIComponent(state.activeRunId)}`);
      const payload = await response.json();
      if (!response.ok || payload.error) throw new Error(payload.error || "采纳状态读取失败");
      setApplyState(payload);
    }

    function setApplyState(payload) {
      state.applyRecords = payload.records || [];
      state.appliedIssueIds = new Set(payload.applied_issue_ids || []);
      state.latestApplyId = payload.latest_apply_id || "";
      state.canRollback = Boolean(payload.can_rollback);
      state.selectedGroups = new Set(
        [...state.selectedGroups].filter(groupId => {
          const group = issueGroups().find(item => item.id === groupId);
          return group && !isGroupApplied(group);
        })
      );
      renderResultStream();
      renderSnapshotBanner();
    }

    function renderSnapshotBanner() {
      const hasApplied = state.applyRecords.some(record => record.status === "applied");
      snapshotBanner.classList.toggle("hidden", !hasApplied);
      rollbackButton.disabled = !state.canRollback || state.applying;
      rollbackButton.title = state.canRollback ? "恢复最近一次采纳前的源码" : "只能撤销该仓库最近一次有效采纳";
    }

    function openApplyDialog(issueIds) {
      if (!state.reportActionable) {
        showToast(state.scanning ? "分析完成后才能采纳修改" : "未完成的临时结果不能采纳，请重新分析", "warning");
        return;
      }
      const unique = [...new Set(issueIds)].filter(Boolean);
      if (!unique.length || !state.activeRunId) {
        showToast("当前问题没有可安全采纳的修改", "warning");
        return;
      }
      const selected = issueGroups().filter(group => patchIssueIds(group).some(id => unique.includes(id)));
      const files = new Set(selected.map(group => group.primary.file_path));
      state.pendingIssueIds = unique;
      applySummary.innerHTML = `将采纳 <strong>${selected.length}</strong> 处精确修改，涉及 <strong>${files.size}</strong> 个文件。<br>写入前会统一校验源码快照，任一修改失效时整批取消。`;
      applyDialog.classList.remove("hidden");
      confirmApplyButton.focus();
    }

    function closeApplyConfirmation() {
      if (state.applying) return;
      applyDialog.classList.add("hidden");
      state.pendingIssueIds = [];
    }

    async function submitApply() {
      if (state.applying || !state.pendingIssueIds.length) return;
      state.applying = true;
      confirmApplyButton.disabled = true;
      confirmApplyButton.textContent = "正在采纳...";
      try {
        const response = await apiFetch("/api/apply", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ run_id: state.activeRunId, issue_ids: state.pendingIssueIds })
        });
        const payload = await response.json();
        if (!response.ok || payload.error) throw new Error(payload.error || "采纳失败");
        applyDialog.classList.add("hidden");
        state.pendingIssueIds = [];
        state.selectedGroups = new Set();
        setApplyState(payload.applies || {});
        showToast("修改已采纳，原文件已保存到用户数据目录", "success");
      } catch (error) {
        showToast(await requestFailureMessage(error, "采纳失败"), "error");
      } finally {
        state.applying = false;
        confirmApplyButton.disabled = false;
        confirmApplyButton.textContent = "确认采纳";
        renderSnapshotBanner();
      }
    }

    async function rollbackLatestApply() {
      if (state.applying || !state.canRollback) return;
      state.applying = true;
      rollbackButton.disabled = true;
      try {
        const response = await apiFetch("/api/apply/rollback", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ apply_id: state.latestApplyId })
        });
        const payload = await response.json();
        if (!response.ok || payload.error) throw new Error(payload.error || "撤销失败");
        setApplyState(payload.applies || {});
        showToast("上次采纳已撤销", "success");
      } catch (error) {
        showToast(await requestFailureMessage(error, "撤销失败"), "error");
      } finally {
        state.applying = false;
        renderSnapshotBanner();
      }
    }

    function showToast(message, type = "info", duration = null) {
      const current = toastRegion.firstElementChild;
      if (current) current.remove();
      const toast = document.createElement("div");
      toast.className = `toast ${type}`;
      toast.setAttribute("role", type === "error" ? "alert" : "status");
      toast.innerHTML = `<div class="toast-message">${esc(message)}</div>`;
      toastRegion.appendChild(toast);
      const hideAfter = duration ?? (type === "error" ? 5200 : type === "warning" ? 3800 : 2800);
      if (hideAfter > 0) {
        window.setTimeout(() => {
          if (!toast.isConnected) return;
          toast.classList.add("leaving");
          window.setTimeout(() => toast.remove(), 170);
        }, hideAfter);
      }
    }

    async function requestFailureMessage(error, action) {
      const detail = String(error?.message || "").trim();
      const networkFailure = error instanceof TypeError || /network|fetch/i.test(detail);
      if (!networkFailure) return `${action}：${detail || "未知错误"}`;
      try {
        const response = await apiFetch("/api/state", { cache: "no-store" });
        if (response.ok) return `${action}，本地服务仍在线，请重试。`;
      } catch (_stateError) {
        // The state probe is the final distinction between an endpoint failure and a stopped service.
      }
      return "本地 LogPilot 服务已退出，请重新启动服务";
    }

    function setScanning(value) {
      state.scanning = value;
      scanButton.disabled = value || !selectedRuntime();
      runtimeSelect.disabled = value;
      scanButton.querySelector("span").textContent = value ? "分析中..." : "开始分析";
      if (!value) {
        state.scanCancelRequested = false;
        cancelScanButton.disabled = true;
      }
      if (state.report) renderResultStream();
    }

    function setBrowsing(value) {
      state.browsing = value;
      browseButton.disabled = value;
      browseButton.querySelector("span").textContent = value ? "选择中..." : "选择仓库";
    }

    function openFullPatch() {
      renderFullPatch(state.patch || "本次分析没有生成安全修改。");
      fullPatchDialog.classList.remove("hidden");
      closePatchDialog.focus();
    }

    function closeFullPatch() {
      fullPatchDialog.classList.add("hidden");
      fullPatchButton.focus();
    }

    function toggleDiagnostics() {
      state.diagnosticsOpen = !state.diagnosticsOpen;
      renderDiagnostics();
    }

    function showTab(name) {
      state.activeView = name;
      currentPanel.classList.toggle("hidden", name !== "current");
      historyPanel.classList.toggle("hidden", name !== "history");
      settingsPanel.classList.toggle("hidden", name !== "settings");
      currentTab.classList.toggle("active", name === "current");
      historyTab.classList.toggle("active", name === "history");
      settingsTab.classList.toggle("active", name === "settings");
    }

    function repositoryName(path) {
      const normalized = String(path || "").replace(/[\\/]+$/, "");
      return normalized.split(/[\\/]/).filter(Boolean).pop() || "未选择仓库";
    }

    function updateRepositoryIdentity(path) {
      state.path = path || state.path;
    }

    function selectedRuntime() {
      return state.runtimes.find(runtime => runtime.id === state.selectedRuntime && runtime.status === "online") || null;
    }

    function updateRuntimeIndicator() {
      const runtime = selectedRuntime();
      runtimeDot.classList.toggle("offline", !runtime);
      scanButton.disabled = state.scanning || !runtime;
      scanButton.title = runtime ? `使用 ${runtime.name} 执行分析` : "没有可用运行时";
    }

    function renderRuntimes() {
      const online = state.runtimes.filter(runtime => runtime.status === "online");
      document.querySelector("#runtimeSummary").textContent = `${online.length} 个在线 · ${state.runtimes.length} 个已检测`;
      runtimeSelect.innerHTML = state.runtimes.length
        ? state.runtimes.map(runtime => `<option value="${esc(runtime.id)}" ${runtime.status !== "online" ? "disabled" : ""}>${esc(runtime.name)} · ${runtime.status === "online" ? "在线" : "离线"}</option>`).join("")
        : '<option value="">未发现运行时</option>';
      runtimeSelect.value = state.selectedRuntime;
      const list = document.querySelector("#runtimeList");
      list.innerHTML = state.runtimes.length ? state.runtimes.map(runtime => `
        <button class="runtime-row ${runtime.id === state.selectedRuntime ? "selected" : ""}" type="button" data-runtime-id="${esc(runtime.id)}" ${runtime.status !== "online" ? "disabled" : ""}>
          <div class="runtime-name"><span class="runtime-logo">${esc(runtime.name.slice(0, 1))}</span><strong>${esc(runtime.name)}</strong><span class="runtime-badge">内置</span></div>
          <div class="health ${esc(runtime.status)}"><span class="state-dot ${runtime.status === "online" ? "" : "offline"}"></span>${runtime.status === "online" ? "在线" : "离线"}</div>
          <div class="runtime-value" title="${esc(runtime.version || runtime.error)}">${esc(runtime.version || "未检测到")}</div>
          <div class="runtime-value" title="${esc(runtime.executable_path || runtime.error)}">${esc(runtime.executable_path || runtime.error)}</div>
        </button>
      `).join("") : '<div class="empty">未检测到 Codex 或 Claude 命令行运行时</div>';
      list.querySelectorAll("button[data-runtime-id]").forEach(button => {
        button.addEventListener("click", () => {
          state.selectedRuntime = button.dataset.runtimeId;
          runtimeSelect.value = state.selectedRuntime;
          window.localStorage.setItem("logpilot.runtime", state.selectedRuntime);
          renderRuntimes();
          showToast(`已选择 ${selectedRuntime().name} 运行时`, "success");
        });
      });
      updateRuntimeIndicator();
    }

    function emptyRepositorySettings() {
      return {
        language_mode: "auto",
        selected_languages: [],
        templates: {},
        language_presets: [],
        template_presets: [],
        active_language_preset: "auto",
        active_template_preset: "auto",
        analysis_depth: "standard"
      };
    }

    function presetCollection(type) {
      return type === "language"
        ? state.repositorySettings.language_presets || []
        : state.repositorySettings.template_presets || [];
    }

    function activePresetId(type) {
      return type === "language"
        ? state.repositorySettings.active_language_preset || "auto"
        : state.repositorySettings.active_template_preset || "auto";
    }

    function resolvedLanguageIds() {
      const detected = (state.languageProfile.detected_languages || [])
        .filter(item => item.recommended)
        .map(item => item.id);
      if (detected.length) return detected;
      return state.settingsLanguages.some(item => item.id === "python")
        ? ["python"]
        : state.settingsLanguages.slice(0, 1).map(item => item.id);
    }

    async function selectAnalysisPreset(type, identifier) {
      if (identifier === "current") return;
      const previous = cloneRepositorySettings();
      applyPreset(type, identifier);
      renderRepositorySettings();
      if (await persistRepositorySettings(true)) {
        const selected = identifier === "auto"
          ? (type === "language" ? "自动识别语言" : "自动匹配模板")
          : presetCollection(type).find(item => item.id === identifier)?.name || "方案";
        showToast(`已启用${selected}`, "success");
      } else {
        state.repositorySettings = previous;
        renderRepositorySettings();
      }
    }

    function applyPreset(type, identifier) {
      if (type === "language") {
        if (identifier === "auto") {
          state.repositorySettings.language_mode = "auto";
          state.repositorySettings.selected_languages = [];
          state.repositorySettings.active_language_preset = "auto";
          return;
        }
        const preset = presetCollection("language").find(item => item.id === identifier);
        if (!preset) return;
        state.repositorySettings.language_mode = "custom";
        state.repositorySettings.selected_languages = [...preset.languages];
        state.repositorySettings.active_language_preset = preset.id;
        return;
      }
      if (identifier === "auto") {
        state.repositorySettings.templates = {};
        state.repositorySettings.active_template_preset = "auto";
        return;
      }
      const preset = presetCollection("template").find(item => item.id === identifier);
      if (!preset) return;
      state.repositorySettings.templates = { ...preset.templates };
      state.repositorySettings.active_template_preset = preset.id;
    }

    function openPresetDialog(type) {
      if (!repoPath.value.trim()) {
        showToast("请先输入或选择本地仓库路径", "warning");
        return;
      }
      state.presetDialogType = type;
      presetDialogTitle.textContent = type === "language" ? "新增语言方案" : "新增模板方案";
      presetDialogDescription.textContent = type === "language"
        ? "保存当前语言组合，后续分析可直接选择"
        : "保存当前日志模板，后续分析可直接选择";
      presetNameInput.value = "";
      presetDialog.classList.remove("hidden");
      setTimeout(() => presetNameInput.focus(), 0);
    }

    function closePresetEditor() {
      presetDialog.classList.add("hidden");
      state.presetDialogType = "";
      presetNameInput.value = "";
    }

    async function createPreset() {
      const type = state.presetDialogType;
      const name = presetNameInput.value.trim();
      if (!type || !name || name.length > 40) {
        showToast("请输入方案名称", "warning");
        return;
      }
      const previous = cloneRepositorySettings();
      const identifier = `${type}-${Date.now()}`;
      if (type === "language") {
        const languages = resolvedLanguageIds();
        if (!languages.length) {
          showToast("当前没有可保存的语言", "warning");
          return;
        }
        state.repositorySettings.language_presets.push({ id: identifier, name, languages });
        applyPreset("language", identifier);
      } else {
        const templates = Object.fromEntries(
          resolvedLanguageIds().map(language => [language, effectiveTemplate(language)]).filter(([, value]) => value)
        );
        if (!Object.keys(templates).length) {
          showToast("当前没有可保存的模板", "warning");
          return;
        }
        state.repositorySettings.template_presets.push({ id: identifier, name, templates });
        applyPreset("template", identifier);
      }
      renderRepositorySettings();
      if (await persistRepositorySettings(true)) {
        closePresetEditor();
        showToast(`方案“${name}”已保存`, "success");
      } else {
        state.repositorySettings = previous;
        renderRepositorySettings();
      }
    }

    async function loadSavedPreset(type, identifier) {
      if (!identifier) return;
      const previous = cloneRepositorySettings();
      applyPreset(type, identifier);
      renderRepositorySettings();
      if (await persistRepositorySettings(true)) showToast("方案已载入", "success");
      else {
        state.repositorySettings = previous;
        renderRepositorySettings();
      }
    }

    async function deleteSavedPreset(type, identifier) {
      if (!identifier) return;
      const preset = presetCollection(type).find(item => item.id === identifier);
      if (!preset) return;
      const previous = cloneRepositorySettings();
      const collectionKey = type === "language" ? "language_presets" : "template_presets";
      state.repositorySettings[collectionKey] = presetCollection(type).filter(item => item.id !== identifier);
      if (activePresetId(type) === identifier) applyPreset(type, "auto");
      renderRepositorySettings();
      if (await persistRepositorySettings(true)) showToast(`方案“${preset.name}”已删除`, "success");
      else {
        state.repositorySettings = previous;
        renderRepositorySettings();
      }
    }

    function cloneRepositorySettings() {
      return JSON.parse(JSON.stringify(state.repositorySettings));
    }

    function renderPresetSelectors() {
      const languagePresets = presetCollection("language");
      const templatePresets = presetCollection("template");
      const languageCustom = state.repositorySettings.language_mode === "custom"
        && activePresetId("language") === "auto";
      const templateCustom = Object.keys(state.repositorySettings.templates || {}).length > 0
        && activePresetId("template") === "auto";
      analysisLanguagePreset.innerHTML = [
        '<option value="auto">自动识别</option>',
        ...(languageCustom ? ['<option value="current">当前自定义</option>'] : []),
        ...languagePresets.map(item => `<option value="${esc(item.id)}">${esc(item.name)}</option>`)
      ].join("");
      analysisTemplatePreset.innerHTML = [
        '<option value="auto">自动匹配</option>',
        ...(templateCustom ? ['<option value="current">当前自定义</option>'] : []),
        ...templatePresets.map(item => `<option value="${esc(item.id)}">${esc(item.name)}</option>`)
      ].join("");
      analysisLanguagePreset.value = languageCustom ? "current" : activePresetId("language");
      analysisTemplatePreset.value = templateCustom ? "current" : activePresetId("template");
      const languageIds = resolvedLanguageIds();
      const languageSummary = languageIds.map(id => {
        const language = languageDefinition(id);
        return language ? `${language.label}${language.support_level === "unsupported" ? "（暂不支持）" : ""}` : id;
      }).join("、") || "等待识别";
      const unrecognizedCount = Object.values(state.languageProfile.unrecognized_extensions || {}).reduce((total, value) => total + Number(value || 0), 0);
      analysisLanguageSummary.textContent = unrecognizedCount ? `${languageSummary} · ${unrecognizedCount} 个未知源码文件` : languageSummary;
      analysisTemplateSummary.textContent = templateCustom
        ? `${Object.keys(state.repositorySettings.templates).length} 种自定义模板`
        : activePresetId("template") === "auto" ? "优先沿用仓库日志风格" : "使用已保存模板";
      renderAnalysisDepth();

      settingsLanguagePreset.innerHTML = '<option value="">选择历史方案</option>'
        + languagePresets.map(item => `<option value="${esc(item.id)}">${esc(item.name)}</option>`).join("");
      settingsTemplatePreset.innerHTML = '<option value="">选择历史方案</option>'
        + templatePresets.map(item => `<option value="${esc(item.id)}">${esc(item.name)}</option>`).join("");
      settingsLanguagePreset.value = activePresetId("language") === "auto" ? "" : activePresetId("language");
      settingsTemplatePreset.value = activePresetId("template") === "auto" ? "" : activePresetId("template");
      updatePresetLibraryActions();
    }

    function renderAnalysisDepth() {
      const depth = state.repositorySettings.analysis_depth || "standard";
      analysisDepth.value = depth;
      analysisDepthSummary.textContent = {
        quick: "优先高风险，最多 100 条日志",
        standard: "完整主流程，限制极端规模",
        deep: "不限制 AI 分析目标数量"
      }[depth] || "完整主流程，限制极端规模";
    }

    function updatePresetLibraryActions() {
      loadLanguagePreset.disabled = state.settingsBusy || !settingsLanguagePreset.value;
      deleteLanguagePreset.disabled = state.settingsBusy || !settingsLanguagePreset.value;
      loadTemplatePreset.disabled = state.settingsBusy || !settingsTemplatePreset.value;
      deleteTemplatePreset.disabled = state.settingsBusy || !settingsTemplatePreset.value;
      saveLanguagePreset.disabled = state.settingsBusy;
      saveTemplatePreset.disabled = state.settingsBusy;
    }

    function renderRepositorySettings(path = repoPath.value) {
      document.querySelector("#settingsRepository").textContent = repositoryName(path);
      const settings = state.repositorySettings || emptyRepositorySettings();
      languageMode.querySelectorAll("button[data-language-mode]").forEach(button => {
        button.classList.toggle("active", button.dataset.languageMode === settings.language_mode);
      });
      const detected = new Map((state.languageProfile.detected_languages || []).map(item => [item.id, item]));
      const detectedLanguages = state.settingsLanguages.filter(language => Number(detected.get(language.id)?.file_count || 0) > 0);
      languageOptions.innerHTML = detectedLanguages.length ? detectedLanguages.map(language => {
        const profile = detected.get(language.id) || {};
        const support = language.support_level === "unsupported" ? "暂不支持" : language.support_level === "limited" ? "有限支持" : "完整支持";
        const stats = `${profile.file_count} 个文件 · ${profile.log_count || 0} 条日志 · ${support}`;
        return `<div class="language-option"><span class="state-dot ${language.support_level === "unsupported" ? "offline" : ""}"></span><span><strong>${esc(language.label)}</strong><span>${esc(stats)}</span></span></div>`;
      }).join("") : '<div class="empty">尚未生成仓库语言画像</div>';
      templateLanguageNav.innerHTML = state.settingsLanguages.map(language => `
        <button class="${language.id === state.templateLanguage ? "active" : ""}" type="button" data-template-language="${esc(language.id)}"><span>${esc(language.label)}</span><span>${esc(templateSourceText(language.id, true))}</span></button>
      `).join("");
      templateInput.value = effectiveTemplate(state.templateLanguage);
      renderTemplateMeta();
      renderPresetSelectors();
      updateSettingsBusy();
    }

    function renderTemplateMeta() {
      const language = languageDefinition(state.templateLanguage);
      templateSource.textContent = templateSourceText(state.templateLanguage, false);
      templateSupport.textContent = language?.automatic_fix
        ? "支持自动补充"
        : language?.support_level === "unsupported" ? "暂不支持解析" : "当前仅分析";
      templateSupport.classList.toggle("ready", Boolean(language?.automatic_fix));
      const recommendation = templateRecommendation(state.templateLanguage);
      useRecommendedTemplate.disabled = state.settingsBusy || !recommendation.template;
    }

    function updateSettingsBusy() {
      saveSettingsButton.disabled = state.settingsBusy;
      profileRepositoryButton.disabled = state.settingsBusy;
      analysisLanguagePreset.disabled = state.settingsBusy;
      analysisTemplatePreset.disabled = state.settingsBusy;
      analysisDepth.disabled = state.settingsBusy;
      addLanguagePreset.disabled = state.settingsBusy;
      addTemplatePreset.disabled = state.settingsBusy;
      saveSettingsButton.textContent = state.settingsBusy ? "处理中..." : "保存设置";
      updatePresetLibraryActions();
    }

    function languageDefinition(languageId) {
      return state.settingsLanguages.find(item => item.id === languageId) || null;
    }

    function templateRecommendation(languageId) {
      return state.languageProfile.template_recommendations?.[languageId] || {};
    }

    function hasFixedTemplate(languageId) {
      return Object.prototype.hasOwnProperty.call(state.repositorySettings.templates || {}, languageId)
        && Boolean(String(state.repositorySettings.templates[languageId] || "").trim());
    }

    function effectiveTemplate(languageId) {
      if (hasFixedTemplate(languageId)) return state.repositorySettings.templates[languageId];
      const recommendation = templateRecommendation(languageId);
      return recommendation.template || languageDefinition(languageId)?.builtin_template || "";
    }

    function templateSourceText(languageId, compact) {
      if (hasFixedTemplate(languageId)) return compact ? "固定" : "用户固定模板";
      const recommendation = templateRecommendation(languageId);
      if (recommendation.source === "repository") return compact ? "推荐" : "仓库推荐模板";
      return compact ? "内置" : "内置安全模板";
    }

    function renderEmpty() {
      state.report = null;
      state.pendingExcludedMappings = [];
      state.reportActionable = false;
      state.patch = "";
      state.activeRunId = "";
      state.selectedGroups = new Set();
      state.expandedGroups = new Set();
      state.collapsedFiles = new Set();
      state.searchQuery = "";
      state.severityFilter = "all";
      state.actionFilter = "all";
      state.resultModules = [];
      state.activeModule = "";
      state.issueOffset = 0;
      state.issueTotal = 0;
      state.appliedIssueIds = new Set();
      state.applyRecords = [];
      resultSearch.value = "";
      resultModule.innerHTML = '<option value="">全部目录</option>';
      renderIssuePager();
      document.querySelector("#metrics").innerHTML = summaryMarkup(null);
      resultsSummary.textContent = "等待分析结果";
      resultStream.innerHTML = '<div class="results-empty">选择仓库并开始分析</div>';
      fullPatchButton.disabled = true;
      expandAllButton.disabled = true;
      collapseAllButton.disabled = true;
      batchApplyButton.disabled = true;
      batchBar.classList.add("hidden");
      snapshotBanner.classList.add("hidden");
      mappingBanner.classList.add("hidden");
      coverageBanner.classList.add("hidden");
      scanProgress.classList.add("hidden");
      incrementalNote.classList.add("hidden");
      updateResultFilters();
      renderDiagnostics();
    }

    function renderNoSourcePlan(plan) {
      renderEmpty();
      state.pendingExcludedMappings = plan?.excluded_mappings || [];
      renderMappingBanner(state.pendingExcludedMappings);
      const discovered = Number(plan?.source_files || 0);
      const message = discovered
        ? `发现 ${discovered} 个源码候选，但没有可分析文件。请检查 .logpilot.yaml 扩展名配置或超大文件限制。`
        : "当前目录未发现源码文件。请确认仓库路径或 .logpilot.yaml 排除规则。";
      resultsSummary.textContent = "未发现可分析源码";
      resultStream.innerHTML = `<div class="results-empty">${esc(message)}</div>`;
      coverageBanner.classList.remove("hidden", "complete", "failure");
      coverageBanner.innerHTML = `<strong>未启动分析</strong>：${esc(message)}`;
    }

    function renderReport(report, incremental = false) {
      const previousGroupIds = new Set(issueGroups().map(group => group.id));
      state.report = report;
      if (!incremental) {
        state.patch = "";
        state.selectedGroups = new Set();
        state.collapsedFiles = new Set();
        state.searchQuery = "";
        state.severityFilter = "all";
        state.actionFilter = "all";
        state.appliedIssueIds = new Set();
        state.applyRecords = [];
        resultSearch.value = "";
        fullPatchButton.disabled = true;
      }
      renderMetrics(report.summary);
      const currentGroups = issueGroups();
      if (incremental) {
        currentGroups.forEach(group => {
          if (!previousGroupIds.has(group.id)) state.expandedGroups.add(group.id);
        });
      } else {
        state.expandedGroups = new Set(currentGroups.map(group => group.id));
      }
      renderResultStream();
      renderDiagnostics();
    }

    function renderMetrics(summary) {
      document.querySelector("#metrics").innerHTML = summaryMarkup(summary);
      renderMappingBanner(state.report?.excluded_mappings || []);
      renderCoverageBanner(summary);
    }

    function renderMappingBanner(mappings) {
      if (!mappings.length) {
        mappingBanner.classList.add("hidden");
        mappingBanner.innerHTML = "";
        return;
      }
      const visible = mappings.slice(0, 5).map(mapping => `
        <span><code>${esc(mapping.path)}</code> &rarr; <code>${esc(mapping.target)}</code> · ${esc(mappingReasonLabel(mapping.reason))}</span>`).join("");
      const remaining = mappings.length > 5 ? `<span>其余 ${mappings.length - 5} 个映射目录已排除。</span>` : "";
      mappingBanner.classList.remove("hidden");
      mappingBanner.innerHTML = `<strong>检测到 ${mappings.length} 个目录映射，已排除</strong><div class="mapping-list">${visible}${remaining}<span>当前分析结果不包含以上目录。</span></div>`;
    }

    function mappingReasonLabel(reason) {
      return ({ junction: "Junction", symlink: "目录符号链接", reparse_point: "重解析目录" })[reason] || "目录映射";
    }

    function summaryMarkup(summary) {
      if (!summary) {
        return `
          <div class="score-panel score-neutral"><div class="score-heading"><span class="metric-label">治理评分</span><span class="score-status">待分析</span></div><div class="score-line"><strong>-</strong><span>/ 100</span></div><div class="score-track" style="--score:0"><i></i></div></div>
          ${metricMarkup("-", "扫描文件")}
          ${metricMarkup("-", "日志调用")}
          ${metricMarkup("-", "发现问题")}
          ${riskMarkup({})}
        `;
      }
      const sev = summary.severity_counts || {};
      const hasScore = summary.score !== null && summary.score !== undefined;
      const scoreValue = hasScore ? summary.score : 0;
      const scoreDisplay = hasScore ? summary.score : "N/A";
      const discovered = summary.discovered_files || summary.files_scanned || 0;
      return `
        <div class="score-panel ${esc(scoreTone(summary))}"><div class="score-heading"><span class="metric-label">治理评分</span><span class="score-status">${esc(scoreLabel(summary))}</span></div><div class="score-line"><strong>${esc(scoreDisplay)}</strong>${hasScore ? "<span>/ 100</span>" : ""}</div><div class="score-track" style="--score:${esc(scoreValue)}"><i></i></div></div>
        ${metricMarkup(`${summary.files_scanned} / ${discovered}`, "分析覆盖")}
        ${metricMarkup(summary.log_count, "日志调用")}
        ${metricMarkup(summary.issue_count, "发现问题")}
        ${riskMarkup(sev)}
      `;
    }

    function metricMarkup(value, label) {
      return `<div class="metric"><span class="metric-label">${esc(label)}</span><strong>${esc(value)}</strong></div>`;
    }

    function riskMarkup(counts) {
      const hasCounts = Object.keys(counts).length > 0;
      return `<div class="risk-panel"><span class="metric-label">风险分布</span><div class="risk-breakdown">
        <div class="risk-stat high-risk"><span>高</span><strong>${esc(hasCounts ? counts.high || 0 : "-")}</strong></div>
        <div class="risk-stat medium-risk"><span>中</span><strong>${esc(hasCounts ? counts.medium || 0 : "-")}</strong></div>
        <div class="risk-stat low-risk"><span>低</span><strong>${esc(hasCounts ? counts.low || 0 : "-")}</strong></div>
      </div></div>`;
    }

    function scoreLabel(summary) {
      const status = summary.score_status;
      if (status === "no_log_samples") return "无日志样本";
      if (status === "insufficient_coverage") return "覆盖不足";
      if (status === "ai_incomplete") return "AI 未完成";
      if (status === "scoped") return summary.analysis_scope === "selected_modules" ? "选定目录评分" : "范围评分";
      if (status === "local_only") return "本地规则";
      const score = summary.score;
      if (score >= 85) return "健康";
      if (score >= 60) return "需关注";
      return "高风险";
    }

    function scoreTone(summary) {
      if (summary.score === null || summary.score === undefined) return summary.score_status === "insufficient_coverage" ? "score-warning" : "score-neutral";
      const score = summary.score;
      if (score >= 85) return "score-healthy";
      if (score >= 60) return "score-warning";
      return "score-danger";
    }

    function renderCoverageBanner(summary) {
      if (!summary) {
        coverageBanner.classList.add("hidden");
        return;
      }
      const languages = summary.language_coverage || [];
      const unsupported = languages.filter(item => item.support_level === "unsupported" && item.discovered_files > 0);
      const failed = languages.filter(item => item.failed_files > 0);
      const parseFailures = state.report?.parse_failures || [];
      const unrecognized = summary.unrecognized_extensions || {};
      const complete = summary.coverage_status === "complete";
      const mappingCount = Number(summary.excluded_mapping_count || 0);
      const unsupportedCount = Number(summary.unsupported_files || 0);
      const fullyHealthyContext = complete && !mappingCount && !unsupportedCount && summary.ai_status === "complete" && ["scored", "scoped"].includes(summary.score_status);
      coverageBanner.classList.toggle("hidden", fullyHealthyContext);
      coverageBanner.classList.toggle("complete", complete && parseFailures.length === 0);
      coverageBanner.classList.toggle("failure", parseFailures.length > 0);
      if (parseFailures.length) {
        const visible = parseFailures.slice(0, 5).map(failure => {
          const kind = ({
            parse_error: "解析错误",
            native_crash: "原生进程崩溃",
            timeout: "解析超时",
            protocol_error: "通信错误",
            worker_start_failed: "进程启动失败"
          })[failure.error_kind] || failure.error_kind;
          const reason = String(failure.message || "未知原因").slice(0, 180);
          return `<span><code>${esc(failure.file_path)}</code> · ${esc(kind)} · ${esc(reason)}</span>`;
        }).join("");
        const remaining = parseFailures.length > 5 ? `<span>其余 ${parseFailures.length - 5} 个失败文件请查看报告。</span>` : "";
        coverageBanner.innerHTML = `<strong>有 ${parseFailures.length} 个文件解析失败</strong><div class="coverage-failure-list">${visible}${remaining}</div>`;
      } else if (summary.coverage_status === "partial" || failed.length) {
        const discovered = summary.discovered_files || 0;
        coverageBanner.innerHTML = `<strong>可分析文件覆盖不足</strong>：已分析 ${esc(summary.files_scanned)} / ${esc(discovered)} 个文件。`;
      } else if (unsupported.length || Object.keys(unrecognized).length || mappingCount) {
        const details = unsupported.map(item => `${item.label} ${item.discovered_files} 个文件`);
        Object.entries(unrecognized).forEach(([extension, count]) => details.push(`未知扩展 ${extension} ${count} 个文件`));
        if (mappingCount) details.push(`${mappingCount} 个映射目录`);
        const detail = details.join("、") || `${unsupportedCount} 个不支持文件`;
        const insightApis = [...new Set((state.report?.language_insights || []).flatMap(item => item.logging_apis || []))].slice(0, 6);
        const insightText = insightApis.length ? ` AI 抽样发现日志接口：${insightApis.map(esc).join("、")}；这些线索不计入覆盖率。` : "";
        const discovered = summary.discovered_files || 0;
        const coverageText = discovered ? `可分析文件覆盖 ${esc(summary.files_scanned)} / ${esc(discovered)}（${esc(Math.round(Number(summary.coverage_ratio || 0) * 100))}%）` : "没有可分析文件";
        coverageBanner.innerHTML = `<strong>${coverageText}</strong>：未纳入分析 ${esc(detail)}。${insightText}`;
      } else if (summary.ai_status === "partial") {
        coverageBanner.innerHTML = `<strong>AI 分析未完整完成</strong>：本地规则结果已保留，建议重新分析失败批次。`;
      } else if (summary.ai_status === "skipped") {
        coverageBanner.innerHTML = `<strong>当前仅完成本地规则分析</strong>：选择在线运行时可获得语义和缺失日志分析。`;
      } else if (summary.score_status === "no_log_samples") {
        coverageBanner.innerHTML = `<strong>未发现日志样本</strong>：结果不会被标记为健康。`;
      } else if (summary.analysis_scope === "selected_modules") {
        const discovered = summary.discovered_files || 0;
        coverageBanner.innerHTML = `<strong>当前为选定目录评分</strong>：全仓覆盖 ${esc(summary.files_scanned)} / ${esc(discovered)} 个源码文件，不生成全仓健康结论。`;
      } else {
        const discovered = summary.discovered_files || summary.files_scanned || 0;
        coverageBanner.innerHTML = `<strong>源码覆盖完整</strong>：已分析 ${esc(summary.files_scanned)} / ${esc(discovered)} 个源码文件。`;
      }
    }

    function renderResultStream() {
      updateResultFilters();
      if (!state.report) return;
      const allGroups = issueGroups();
      const files = groupedFiles();
      const visibleCount = files.reduce((total, file) => total + file.groups.length, 0);
      resultsSummary.innerHTML = `<strong>当前页 ${visibleCount} 项</strong> · 共 ${state.issueTotal || allGroups.length} 项 · ${files.length} 个文件`;
      const emptyLabel = state.actionFilter === "all"
        ? "没有匹配的分析结果"
        : `没有${actionTypeText(state.actionFilter)}类分析结果`;
      resultStream.innerHTML = files.length
        ? files.map(fileGroupMarkup).join("")
        : `<div class="results-empty">${esc(emptyLabel)}</div>`;
      resultStream.querySelectorAll("input[data-file-check]").forEach(input => {
        const file = files.find(item => item.path === input.dataset.fileCheck);
        const applicable = file ? file.groups.filter(isGroupApplicable) : [];
        const selectedCount = applicable.filter(group => state.selectedGroups.has(group.id)).length;
        input.checked = applicable.length > 0 && selectedCount === applicable.length;
        input.indeterminate = selectedCount > 0 && selectedCount < applicable.length;
      });
      const visibleGroups = files.flatMap(file => file.groups);
      expandAllButton.disabled = !visibleGroups.length || visibleGroups.every(group => state.expandedGroups.has(group.id));
      collapseAllButton.disabled = !visibleGroups.some(group => state.expandedGroups.has(group.id));
      renderBatchBar();
    }

    function issueGroups() {
      const issues = (state.report && state.report.issues) || [];
      const logs = (state.report && state.report.logs) || [];
      const logsById = new Map(logs.map(log => [log.id, log]));
      const severityRank = { high: 3, medium: 2, low: 1 };
      const groups = new Map();
      issues.forEach(issue => {
        const id = issue.log_call_id || issue.fix?.id || issue.id;
        if (!groups.has(id)) groups.set(id, { id, issues: [], primary: issue, log: logsById.get(issue.log_call_id) || null });
        const group = groups.get(id);
        group.issues.push(issue);
        if ((severityRank[issue.severity] || 0) > (severityRank[group.primary.severity] || 0)) group.primary = issue;
      });
      return [...groups.values()].map(group => {
        const actionType = groupActionType(group);
        return {
          ...group,
          actionType,
          filePath: group.primary.file_path,
          line: Number(group.primary.line || 0),
          searchText: [
            group.primary.file_path,
            group.primary.line,
            actionTypeText(actionType),
            ...group.issues.flatMap(issue => [issue.title, ruleText(issue.kind), issue.reason, issue.suggestion])
          ].join(" ").toLocaleLowerCase("zh-CN")
        };
      });
    }

    function visibleIssueGroups() {
      const query = state.searchQuery.trim().toLocaleLowerCase("zh-CN");
      return issueGroups().filter(group => {
        const severityMatches = state.severityFilter === "all" || group.primary.severity === state.severityFilter;
        const actionMatches = state.actionFilter === "all" || group.actionType === state.actionFilter;
        return severityMatches && actionMatches && (!query || group.searchText.includes(query));
      });
    }

    function groupedFiles() {
      const severityRank = { high: 3, medium: 2, low: 1 };
      const files = new Map();
      visibleIssueGroups().forEach(group => {
        if (!files.has(group.filePath)) files.set(group.filePath, { path: group.filePath, groups: [], maxRank: 0 });
        const file = files.get(group.filePath);
        file.groups.push(group);
        file.maxRank = Math.max(file.maxRank, severityRank[group.primary.severity] || 0);
      });
      return [...files.values()].map(file => ({
        ...file,
        groups: file.groups.sort((left, right) =>
          (severityRank[right.primary.severity] || 0) - (severityRank[left.primary.severity] || 0) || left.line - right.line
        )
      })).sort((left, right) => right.maxRank - left.maxRank || left.path.localeCompare(right.path, "zh-CN"));
    }

    function patchIssueIds(group) {
      return group ? [...new Set(group.issues.filter(issue => issue.fix?.id).map(issue => issue.id))] : [];
    }

    function isGroupApplied(group) {
      return group.issues.some(issue => state.appliedIssueIds.has(issue.id));
    }

    function isGroupApplicable(group) {
      return state.reportActionable && !state.scanning && patchIssueIds(group).length > 0 && !isGroupApplied(group);
    }

    function issueActionType(issue) {
      const fixAction = issue.fix?.action;
      if (fixAction === "delete") return "delete";
      if (fixAction === "insert_before") return "add";
      if (fixAction === "replace") return issue.log_call_id ? "modify" : "add";
      if (issue.kind === "missing_exception_log" || issue.kind === "ai_missing_log") return "add";
      if (issue.patch_action === "delete" || issue.kind === "debug_log") return "delete";
      return "modify";
    }

    function groupActionType(group) {
      const exact = group.issues.find(issue => issue.fix?.id);
      if (exact) return issueActionType(exact);
      const actions = new Set(group.issues.map(issueActionType));
      if (actions.has("add")) return "add";
      if (actions.has("delete")) return "delete";
      return "modify";
    }

    function updateResultFilters() {
      severityFilters.querySelectorAll("button[data-severity]").forEach(button => {
        const active = button.dataset.severity === state.severityFilter;
        button.classList.toggle("active", active);
        button.setAttribute("aria-pressed", active ? "true" : "false");
      });
      const groups = issueGroups();
      const counts = { all: groups.length, add: 0, delete: 0, modify: 0 };
      groups.forEach(group => { counts[group.actionType] = (counts[group.actionType] || 0) + 1; });
      actionFilters.querySelectorAll("button[data-action]").forEach(button => {
        const action = button.dataset.action;
        const active = action === state.actionFilter;
        button.classList.toggle("active", active);
        button.setAttribute("aria-pressed", active ? "true" : "false");
        const count = button.querySelector("[data-action-count]");
        if (count) count.textContent = String(counts[action] || 0);
      });
    }

    function renderBatchBar() {
      const selected = issueGroups().filter(group => state.selectedGroups.has(group.id) && isGroupApplicable(group));
      const files = new Set(selected.map(group => group.filePath));
      batchBar.classList.toggle("hidden", !selected.length);
      batchSelectionCount.textContent = `已选择 ${selected.length} 项`;
      batchSelectionFiles.textContent = `${files.size} 个文件`;
      batchApplyButton.disabled = !selected.length || state.applying;
      batchApplyButton.textContent = selected.length ? `批量采纳（${selected.length}）` : "批量采纳";
    }

    function fileGroupMarkup(file) {
      const collapsed = state.collapsedFiles.has(file.path);
      const exact = file.groups.filter(group => patchIssueIds(group).length > 0 && !isGroupApplied(group));
      const applicable = file.groups.filter(isGroupApplicable);
      const previewOnly = !state.reportActionable && exact.length > 0;
      const allExpanded = file.groups.every(group => state.expandedGroups.has(group.id));
      const anyExpanded = file.groups.some(group => state.expandedGroups.has(group.id));
      return `
        <section class="file-group ${collapsed ? "collapsed" : ""}" data-file-group="${esc(file.path)}">
          <div class="file-group-header">
            <button class="file-toggle" type="button" data-file-toggle="${esc(file.path)}" aria-expanded="${collapsed ? "false" : "true"}">
              <svg class="icon file-caret" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>
              <svg class="icon file-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5z"/><polyline points="14 2 14 8 20 8"/></svg>
              <span><span class="file-path">${esc(file.path)}</span><span class="file-count">${file.groups.length} 个问题位置 · ${exact.length} 项可采纳</span></span>
            </button>
            <div class="file-header-actions">
              <div class="file-fold-actions" role="group" aria-label="${esc(file.path)} 批量展开与折叠">
                <button class="secondary icon-only file-fold-button" type="button" data-file-expand-all="${esc(file.path)}" title="展开该文件全部问题" aria-label="展开该文件全部问题" ${allExpanded && !collapsed ? "disabled" : ""}><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m7 7 5 5 5-5"/><path d="m7 13 5 5 5-5"/></svg></button>
                <button class="secondary icon-only file-fold-button" type="button" data-file-collapse-all="${esc(file.path)}" title="折叠该文件全部问题" aria-label="折叠该文件全部问题" ${!anyExpanded ? "disabled" : ""}><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m7 17 5-5 5 5"/><path d="m7 11 5-5 5 5"/></svg></button>
              </div>
              <label class="file-select" title="${previewOnly ? state.scanning ? "分析完成后可选择" : "未完成的临时结果仅供预览" : applicable.length ? "选择该文件的全部精确修改" : "该文件没有精确修改"}"><input type="checkbox" data-file-check="${esc(file.path)}" ${applicable.length ? "" : "disabled"}>${previewOnly ? state.scanning ? "等待完成" : "仅预览" : applicable.length ? "选择可采纳项" : "无精确修改"}</label>
            </div>
          </div>
          <div class="file-results">${file.groups.map(resultItemMarkup).join("")}</div>
        </section>`;
    }

    function resultItemMarkup(group) {
      const issue = group.primary;
      const expanded = state.expandedGroups.has(group.id);
      const applied = isGroupApplied(group);
      const applicable = isGroupApplicable(group);
      const pending = !state.reportActionable && patchIssueIds(group).length > 0 && !applied;
      const pendingLabel = state.scanning ? "分析完成后可采纳" : "临时结果仅供预览";
      const pendingStatus = state.scanning ? "分析中" : "仅预览";
      const rules = [...new Set(group.issues.map(item => ruleText(item.kind)).filter(Boolean))];
      const reasons = uniqueText(group.issues.map(item => item.reason));
      const suggestions = uniqueText(group.issues.map(item => item.suggestion));
      const fixIssue = group.issues.find(item => item.fix?.id);
      const fix = fixIssue?.fix || null;
      return `
        <article class="result-item ${expanded ? "expanded" : ""} ${state.selectedGroups.has(group.id) ? "selected" : ""}">
          <div class="result-item-header">
            <label class="issue-select" title="${applicable ? "选择此修改" : pending ? pendingLabel : applied ? "该修改已采纳" : "当前问题没有精确修改"}"><input type="checkbox" data-group-check="${esc(group.id)}" ${state.selectedGroups.has(group.id) ? "checked" : ""} ${applicable ? "" : "disabled"}></label>
            <span class="pill ${esc(issue.severity)}">${esc(severityText(issue.severity))}</span>
            <button class="result-toggle" type="button" data-group-toggle="${esc(group.id)}" aria-expanded="${expanded ? "true" : "false"}">
              <span class="result-title-line"><span class="result-title">${esc(issue.title)}</span><span class="action-chip ${esc(group.actionType)}">${esc(actionTypeText(group.actionType))}</span>${applied ? '<span class="issue-status">已采纳</span>' : pending ? `<span class="issue-status muted">${pendingStatus}</span>` : !fix ? '<span class="issue-status muted">仅建议</span>' : ""}</span>
              <span class="result-rules">第 ${esc(issue.line)} 行 · ${esc(rules.join("、"))} · ${esc(sourceText(issue.source))}</span>
            </button>
            <svg class="icon result-caret" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>
          </div>
          ${expanded ? `
            <div class="result-content">
              <div class="finding-copy">
                <div class="copy-row"><span>原因</span><div>${reasons.map(value => `<p>${esc(value)}</p>`).join("") || "未提供"}</div></div>
                <div class="copy-row"><span>建议</span><div>${suggestions.map(value => `<p>${esc(value)}</p>`).join("") || "未提供"}</div></div>
              </div>
              <div class="inline-block"><div class="inline-block-header"><span>相关代码</span><span>${esc(issue.file_path)}:${esc(issue.line)}</span></div><div class="code-view">${codeContextMarkup(relatedCodeTextFor(group))}</div></div>
              ${fix ? `<div class="inline-block"><div class="inline-block-header"><span>修改预览</span><span>${esc(fixActionText(fix))} · ${esc(fixSourceText(fix.source))}</span></div><div class="diff-view inline-diff">${diffMarkup(fixPreview(fix))}</div></div>` : ""}
              ${fix ? `<div class="result-footer"><button type="button" data-apply-group="${esc(group.id)}" ${applicable ? "" : "disabled"}>${applied ? "已采纳" : pending ? pendingLabel : "采纳此修改"}</button></div>` : ""}
            </div>` : ""}
        </article>`;
    }

    function uniqueText(values) {
      return [...new Set(values.map(value => String(value || "").trim()).filter(Boolean))];
    }

    function relatedCodeTextFor(group) {
      const issue = group.primary;
      const context = issue.context || (group.log && group.log.context) || "";
      if (!context) return "当前报告没有保存源码上下文，请重新运行分析。";
      return context.split("\\n").map(line =>
        line.trimStart().startsWith(String(issue.line) + ":") ? "> " + line : "  " + line
      ).join("\\n");
    }

    function codeContextMarkup(value) {
      return String(value || "").split("\\n").map(line => {
        const target = line.startsWith("> ");
        const content = target || line.startsWith("  ") ? line.slice(2) : line;
        return `<span class="code-line ${target ? "target" : ""}">${esc(content || " ")}</span>`;
      }).join("");
    }

    function diffMarkup(value) {
      return String(value || "").split("\\n").map(line => {
        let type = "context";
        if (line.startsWith("--- ") || line.startsWith("+++ ")) type = "file";
        else if (line.startsWith("@@")) type = "hunk";
        else if (line.startsWith("+")) type = "add";
        else if (line.startsWith("-")) type = "remove";
        else if (line.startsWith("\\\\") || line.startsWith("#")) type = "note";
        return `<span class="diff-line ${type}">${esc(line || " ")}</span>`;
      }).join("");
    }

    function fixPreview(fix) {
      const removed = String(fix.expected_text || "").split("\\n").map(line => `- ${line}`).join("\\n");
      const added = String(fix.replacement_text || "").split("\\n").filter(line => line.length).map(line => `+ ${line}`).join("\\n");
      if (fix.action === "delete") return removed;
      if (fix.action === "replace") return `${removed}\\n${added}`;
      return added;
    }

    function fixActionText(fix) {
      return ({ delete: "删除日志", replace: "替换代码", insert_before: "补充日志" })[fix.action] || "修改代码";
    }

    function fixSourceText(source) {
      return ({ fixed: "固定模板", repository: "仓库风格", builtin: "内置模板", rule: "规则生成" })[source] || "精确修改";
    }

    function renderFullPatch(value) {
      fullPatchPre.innerHTML = diffMarkup(value);
    }

    function handleResultStreamClick(event) {
      const fileExpand = event.target.closest("button[data-file-expand-all]");
      if (fileExpand) {
        setFileGroupsExpanded(fileExpand.dataset.fileExpandAll, true);
        return;
      }
      const fileCollapse = event.target.closest("button[data-file-collapse-all]");
      if (fileCollapse) {
        setFileGroupsExpanded(fileCollapse.dataset.fileCollapseAll, false);
        return;
      }
      const fileToggle = event.target.closest("button[data-file-toggle]");
      if (fileToggle) {
        const path = fileToggle.dataset.fileToggle;
        if (state.collapsedFiles.has(path)) state.collapsedFiles.delete(path);
        else state.collapsedFiles.add(path);
        renderResultStream();
        return;
      }
      const groupToggle = event.target.closest("button[data-group-toggle]");
      if (groupToggle) {
        const id = groupToggle.dataset.groupToggle;
        if (state.expandedGroups.has(id)) state.expandedGroups.delete(id);
        else state.expandedGroups.add(id);
        renderResultStream();
        return;
      }
      const applyButton = event.target.closest("button[data-apply-group]");
      if (applyButton) {
        const group = issueGroups().find(item => item.id === applyButton.dataset.applyGroup);
        if (group) openApplyDialog(patchIssueIds(group));
      }
    }

    function setVisibleGroupsExpanded(expanded) {
      const groups = visibleIssueGroups();
      groups.forEach(group => {
        if (expanded) state.expandedGroups.add(group.id);
        else state.expandedGroups.delete(group.id);
        if (expanded) state.collapsedFiles.delete(group.filePath);
      });
      renderResultStream();
    }

    function setFileGroupsExpanded(path, expanded) {
      const file = groupedFiles().find(item => item.path === path);
      if (!file) return;
      file.groups.forEach(group => {
        if (expanded) state.expandedGroups.add(group.id);
        else state.expandedGroups.delete(group.id);
      });
      if (expanded) state.collapsedFiles.delete(path);
      renderResultStream();
    }

    function handleResultStreamChange(event) {
      const groupInput = event.target.closest("input[data-group-check]");
      if (groupInput) {
        if (groupInput.checked) state.selectedGroups.add(groupInput.dataset.groupCheck);
        else state.selectedGroups.delete(groupInput.dataset.groupCheck);
        renderResultStream();
        return;
      }
      const fileInput = event.target.closest("input[data-file-check]");
      if (!fileInput) return;
      const file = groupedFiles().find(item => item.path === fileInput.dataset.fileCheck);
      if (!file) return;
      file.groups.filter(isGroupApplicable).forEach(group => {
        if (fileInput.checked) state.selectedGroups.add(group.id);
        else state.selectedGroups.delete(group.id);
      });
      renderResultStream();
    }

    function renderDiagnostics() {
      const traces = (state.report && state.report.ai_traces) || [];
      const summary = document.querySelector("#diagnosticsSummary");
      if (!traces.length) state.diagnosticsOpen = false;
      diagnosticsToggle.disabled = !traces.length;
      diagnosticsToggle.querySelector("span").textContent = traces.length
        ? (state.diagnosticsOpen ? "收起诊断" : "查看诊断")
        : "暂无诊断";
      summary.textContent = traces.length
        ? traces.length + " 条运行记录，仅用于排查模型分析异常"
        : "当前结果未包含模型运行记录";
      diagnosticsPre.classList.toggle("hidden", !state.diagnosticsOpen || !traces.length);
      diagnosticsPre.textContent = traces.map(trace => [
        "运行时  " + (trace.runtime_id || "未知"),
        "版本    " + (trace.runtime_version || "未知"),
        "耗时    " + (trace.duration_ms || 0) + " ms",
        "状态    " + trace.status,
        "",
        "请求\\n" + (trace.prompt || "无请求内容"),
        "",
        "返回\\n" + (trace.error || trace.raw_response || "无返回内容")
      ].join("\\n")).join("\\n\\n----------------\\n\\n");
    }

    function severityText(value) {
      if (value === "high") return "高";
      if (value === "medium") return "中";
      if (value === "low") return "低";
      return value;
    }

    function actionTypeText(value) {
      if (value === "add") return "增加日志";
      if (value === "delete") return "删除日志";
      if (value === "modify") return "修改日志";
      return "全部动作";
    }

    function sourceText(value) {
      if (value === "rule") return "规则分析";
      if (String(value).startsWith("runtime:")) return `${String(value).slice(8)} 运行时`;
      return value || "未知来源";
    }

    function ruleText(value) {
      return ({
        forbidden_log: "禁用接口",
        debug_log: "调试日志",
        low_value_log: "低价值信息",
        sensitive_log: "敏感数据",
        duplicate_log: "重复信息",
        missing_exception_log: "异常记录",
        ai_log_quality: "AI 质量分析",
        ai_missing_log: "AI 缺失分析"
      })[value] || value;
    }

    function renderHistory(runs) {
      const target = document.querySelector("#historyList");
      if (!runs.length) {
        target.innerHTML = '<div class="empty">暂无历史记录</div>';
        return;
      }
      target.innerHTML = runs.map(run => {
        const sev = run.severity_counts || {};
        return `
          <div class="item history-row">
            <div>
              <h3>${esc(repositoryName(run.repository))}</h3>
              <div class="meta">${esc(formatTime(run.created_at))} · ${esc(run.repository)}</div>
            </div>
            <div class="history-score"><strong>${esc(run.score ?? "N/A")}</strong>${run.score === null || run.score === undefined ? "" : "<span> / 100</span>"}</div>
            <div class="history-stats">${esc(run.files_scanned)} / ${esc(run.discovered_files || run.files_scanned)} 文件 · ${esc(run.log_count)} 日志 · ${esc(run.issue_count)} 问题<br>${esc(run.runtime_id || "规则分析")} · 高 ${esc(sev.high || 0)} · 中 ${esc(sev.medium || 0)} · 低 ${esc(sev.low || 0)}</div>
            <button class="secondary" type="button" data-run-id="${esc(run.run_id)}" data-run-status="${esc(run.status || "completed")}"><span>${run.status === "interrupted" ? "继续" : "查看"}</span><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg></button>
          </div>
        `;
      }).join("");
      target.querySelectorAll("button[data-run-id]").forEach(button => {
        button.addEventListener("click", () => button.dataset.runStatus === "interrupted"
          ? resumeHistoryRun(button.dataset.runId)
          : loadHistoryRun(button.dataset.runId));
      });
    }

    async function resumeHistoryRun(runId) {
      const runtime = selectedRuntime();
      if (!runtime) {
        showToast("没有可用运行时，无法继续分析", "warning");
        return;
      }
      resetReportForScan();
      setScanning(true);
      try {
        const response = await apiFetch("/api/scans", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: state.path, runtime: runtime.id, resume_run_id: runId })
        });
        const payload = await response.json();
        if (!response.ok || payload.error) throw new Error(payload.error || "继续分析失败");
        state.scanJobId = payload.job.job_id;
        state.activeRunId = runId;
        renderScanProgress(payload.job);
        showTab("current");
        await pollScanJob(runtime);
      } catch (error) {
        setScanning(false);
        showToast(await requestFailureMessage(error, "继续分析失败"), "error");
      }
    }

    function formatTime(value) {
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return value || "未知时间";
      return date.toLocaleString("zh-CN", { hour12: false });
    }

    init();
}
