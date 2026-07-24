/* ==========================================================================
   google2api Vue 3 SPA Core Application Logic
   ========================================================================== */

const { createApp, ref, reactive, computed, onMounted, onUnmounted, watch, nextTick } = Vue;

const app = createApp({
    setup() {
        // ----------------------------------------------------------------------
        // 1. 全局状态与基础控制
        // ----------------------------------------------------------------------
        const token = ref(localStorage.getItem('google2api_panel_password') || '');
        const loginPassword = ref('');
        const isLoggedIn = ref(false);
        const activeTab = ref(localStorage.getItem('google2api_active_tab') || 'antigravity');
        const theme = ref(localStorage.getItem('google2api_theme') || 'light');

        const isSidebarCollapsed = ref(localStorage.getItem('google2api_sidebar_collapsed') === 'true');
        const isDrawerOpen = ref(false);

        // Toast 状态框
        const toast = reactive({
            show: false,
            message: '',
            type: 'info',
            timeout: null
        });

        // 模态对话框 (自定义 Alert & Confirm 弹窗系统)
        const modal = reactive({
            show: false,
            title: '',
            message: '',
            type: 'info',
            isConfirm: false,
            confirmText: '确定',
            cancelText: '取消',
            resolve: null
        });

        const showStatus = (message, type = 'info') => {
            if (toast.timeout) clearTimeout(toast.timeout);
            toast.message = message;
            toast.type = type;
            toast.show = true;
            toast.timeout = setTimeout(() => {
                toast.show = false;
            }, 3500);
        };

        const copyToClipboard = async (text, successMsg = '已复制到剪贴板') => {
            if (!text) return;
            try {
                if (navigator.clipboard && window.isSecureContext) {
                    await navigator.clipboard.writeText(text);
                } else {
                    const textArea = document.createElement('textarea');
                    textArea.value = text;
                    textArea.style.position = 'fixed';
                    textArea.style.opacity = '0';
                    document.body.appendChild(textArea);
                    textArea.focus();
                    textArea.select();
                    document.execCommand('copy');
                    document.body.removeChild(textArea);
                }
                showStatus(`✅ ${successMsg}`, 'success');
            } catch (err) {
                showStatus('❌ 复制失败，请手动选择复制', 'error');
            }
        };

        const showAlert = (message, title = '提示', type = 'info') => {
            return new Promise((resolve) => {
                modal.title = title;
                modal.message = message;
                modal.type = type;
                modal.isConfirm = false;
                modal.confirmText = '确定';
                modal.cancelText = '取消';
                modal.resolve = resolve;
                modal.show = true;
            });
        };

        const showConfirm = (message, title = '操作确认', options = {}) => {
            return new Promise((resolve) => {
                modal.title = title;
                modal.message = message;
                modal.type = options.type || 'warning';
                modal.isConfirm = true;
                modal.confirmText = options.confirmText || '确定';
                modal.cancelText = options.cancelText || '取消';
                modal.resolve = resolve;
                modal.show = true;
            });
        };

        const handleModalConfirm = () => {
            modal.show = false;
            if (modal.resolve) {
                const res = modal.resolve;
                modal.resolve = null;
                res(true);
            }
        };

        const handleModalCancel = () => {
            modal.show = false;
            if (modal.resolve) {
                const res = modal.resolve;
                modal.resolve = null;
                res(false);
            }
        };

        const showMessageModal = (title, message, type = 'info') => {
            return showAlert(message, title, type);
        };

        const handleKeyDown = (e) => {
            if (modal.show) {
                if (e.key === 'Escape') {
                    handleModalCancel();
                } else if (e.key === 'Enter') {
                    handleModalConfirm();
                }
            }
        };

        onMounted(() => {
            window.addEventListener('keydown', handleKeyDown);
        });

        onUnmounted(() => {
            window.removeEventListener('keydown', handleKeyDown);
        });

        const getAuthHeaders = () => {
            return {
                'Authorization': `Bearer ${token.value}`,
                'Content-Type': 'application/json'
            };
        };

        // 主题切换
        const initTheme = () => {
            if (theme.value === 'dark') {
                document.body.classList.add('dark-theme');
            } else {
                document.body.classList.remove('dark-theme');
            }
        };

        const toggleTheme = () => {
            theme.value = theme.value === 'dark' ? 'light' : 'dark';
            localStorage.setItem('google2api_theme', theme.value);
            initTheme();
        };

        const toggleSidebarCollapse = () => {
            isSidebarCollapsed.value = !isSidebarCollapsed.value;
            localStorage.setItem('google2api_sidebar_collapsed', isSidebarCollapsed.value);
        };

        const toggleDrawer = () => {
            isDrawerOpen.value = !isDrawerOpen.value;
        };

        const switchTab = (tabName) => {
            activeTab.value = tabName;
            localStorage.setItem('google2api_active_tab', tabName);
            isDrawerOpen.value = false;

            if (tabName === 'antigravity') fetchCreds('antigravity');
            if (tabName === 'oauth') fetchCreds('oauth');
            if (tabName === 'tokens') {
                loadTokenDashboard();
                nextTick(() => {
                    renderTokenTrendChart();
                });
            }
            if (tabName === 'models') loadModelMappings();
            if (tabName === 'config') loadConfig();
        };

        // ----------------------------------------------------------------------
        // 2. 凭证数据模型 (Antigravity & OAuth)
        // ----------------------------------------------------------------------
        const createCredManager = (modeType) => {
            return reactive({
                items: [],
                total: 0,
                stats: { total: 0, normal: 0, disabled: 0 },
                currentPage: 1,
                pageSize: 20,
                statusFilter: 'all',
                errorCodeFilter: 'all',
                cooldownFilter: 'all',
                tierFilter: 'all',
                previewFilter: 'all',
                viewMode: localStorage.getItem(`google2api_${modeType}_view_mode`) || 'card',
                selectedFiles: [],
                authPanelVisible: false,
                authUrl: '#',
                projectIdInput: '',
                projectIdSectionVisible: false,
                callbackUrlInput: '',
                callbackSectionVisible: false,
                expandedDetails: {},
                expandedErrors: {},
                currentSelected: null,
                currentSelectedTime: null,
                loading: false
            });
        };

        const ag = createCredManager('antigravity');
        const oauth = createCredManager('oauth');

        const getManager = (type) => (type === 'antigravity' ? ag : oauth);

        const getSelectedEmail = (type) => {
            const m = getManager(type);
            if (!m.currentSelected) return '';
            const found = m.items.find(item => item.filename === m.currentSelected || item.is_selected);
            if (found) {
                return found.user_email || found.user_name || found.filename;
            }
            return m.currentSelected;
        };

        // 获取凭证列表
        const fetchCreds = async (type, silent = false) => {
            const m = getManager(type);
            m.loading = true;
            try {
                const offset = (m.currentPage - 1) * m.pageSize;
                const modeParam = type === 'antigravity' ? 'mode=antigravity' : 'mode=geminicli';
                const url = `./creds/status?offset=${offset}&limit=${m.pageSize}&status_filter=${m.statusFilter}&error_code_filter=${m.errorCodeFilter}&cooldown_filter=${m.cooldownFilter}&preview_filter=${m.previewFilter}&tier_filter=${m.tierFilter}&${modeParam}`;

                const response = await fetch(url, { headers: getAuthHeaders() });
                const data = await response.json();

                if (response.ok) {
                    m.currentSelected = data.current_selected || null;
                    m.currentSelectedTime = data.current_selected_time || null;
                    m.items = (data.items || []).map(item => ({
                        ...item,
                        pathId: (type === 'antigravity' ? 'ag_' : '') + btoa(encodeURIComponent(item.filename)).replace(/[+/=]/g, '_')
                    }));
                    m.total = data.total || 0;
                    if (data.stats) {
                        m.stats = data.stats;
                    }
                    if (!silent) {
                        let msg = `已加载 ${m.total} 个${type === 'antigravity' ? 'Antigravity' : ''}凭证文件`;
                        if (m.statusFilter !== 'all') {
                            msg += ` (筛选: ${m.statusFilter === 'enabled' ? '仅启用' : '仅禁用'})`;
                        }
                        showStatus(msg, 'success');
                    }
                } else {
                    showStatus(`加载失败: ${data.detail || data.error || '未知错误'}`, 'error');
                }
            } catch (err) {
                showStatus(`网络错误: ${err.message}`, 'error');
            } finally {
                m.loading = false;
            }
        };

        // 视图模式切换
        watch(() => ag.viewMode, (newVal) => localStorage.setItem('google2api_antigravity_view_mode', newVal));
        watch(() => oauth.viewMode, (newVal) => localStorage.setItem('google2api_oauth_view_mode', newVal));

        // 选框控制
        const isAllSelected = (type) => {
            const m = getManager(type);
            if (m.items.length === 0) return false;
            return m.items.every(item => m.selectedFiles.includes(item.filename));
        };

        const toggleSelectAll = (type) => {
            const m = getManager(type);
            if (isAllSelected(type)) {
                m.selectedFiles = [];
            } else {
                m.selectedFiles = m.items.map(item => item.filename);
            }
        };

        // 单个操作
        const singleAction = async (type, filename, action) => {
            const modeParam = type === 'antigravity' ? 'mode=antigravity' : 'mode=geminicli';
            try {
                const response = await fetch(`./creds/action?${modeParam}`, {
                    method: 'POST',
                    headers: getAuthHeaders(),
                    body: JSON.stringify({ filename, action })
                });
                const data = await response.json();
                if (response.ok) {
                    showStatus(data.message || `操作成功: ${action}`, 'success');
                    await fetchCreds(type, true);
                } else {
                    showStatus(`操作失败: ${data.detail || data.error || '未知错误'}`, 'error');
                }
            } catch (err) {
                showStatus(`网络错误: ${err.message}`, 'error');
            }
        };

        const deleteSingleCredential = async (type, filename) => {
            if (!await showConfirm(`确定要删除 ${filename} 凭证文件吗？\n此操作不可恢复！`, '删除确认', { type: 'danger', confirmText: '确认删除' })) return;
            await singleAction(type, filename, 'delete');
        };

        // 批量操作
        const batchAction = async (type, action) => {
            const m = getManager(type);
            if (m.selectedFiles.length === 0) {
                showStatus('请先选择要操作的文件', 'error');
                return;
            }
            const actionNames = { enable: '启用', disable: '禁用', delete: '删除', enable_credit: '开启积分', disable_credit: '关闭积分' };
            const label = actionNames[action] || action;
            if (!await showConfirm(`确定要${label}选中的 ${m.selectedFiles.length} 个文件吗？`, '批量操作确认', { type: action === 'delete' ? 'danger' : 'warning' })) return;

            try {
                showStatus(`正在执行批量${label}操作...`, 'info');
                const modeParam = type === 'antigravity' ? 'mode=antigravity' : 'mode=geminicli';
                const response = await fetch(`./creds/batch-action?${modeParam}`, {
                    method: 'POST',
                    headers: getAuthHeaders(),
                    body: JSON.stringify({ action, filenames: m.selectedFiles })
                });
                const data = await response.json();
                if (response.ok) {
                    showStatus(`批量操作完成：成功处理 ${data.success_count || data.succeeded} 个文件`, 'success');
                    m.selectedFiles = [];
                    await fetchCreds(type, true);
                } else {
                    showStatus(`批量操作失败: ${data.detail || data.error || '未知错误'}`, 'error');
                }
            } catch (err) {
                showStatus(`网络错误: ${err.message}`, 'error');
            }
        };

        // 手动调度切换
        const switchActiveCredential = async (type, filename) => {
            try {
                showStatus(`🎯 正在手动调度到 ${filename}...`, 'info');
                const modeParam = type === 'antigravity' ? '?mode=antigravity' : '';
                const response = await fetch(`./creds/switch/${encodeURIComponent(filename)}${modeParam}`, {
                    method: 'POST',
                    headers: getAuthHeaders()
                });
                const data = await response.json();
                if (response.ok) {
                    showStatus(`🎯 ${data.message || '手动调度成功！'}`, 'success');
                    await fetchCreds(type, true);
                } else {
                    showStatus(`❌ ${data.detail || data.message || '手动调度失败'}`, 'error');
                }
            } catch (err) {
                showStatus(`❌ 手动调度异常: ${err.message}`, 'error');
            }
        };

        // 检验 Project ID
        const verifyProjectId = async (type, filename) => {
            try {
                showStatus('🔍 正在检验 Project ID，请稍候...', 'info');
                const modeParam = type === 'antigravity' ? '?mode=antigravity' : '';
                const response = await fetch(`./creds/verify-project/${encodeURIComponent(filename)}${modeParam}`, {
                    method: 'POST',
                    headers: getAuthHeaders()
                });
                const data = await response.json();

                if (response.ok && data.success) {
                    const tierLine = data.subscription_tier ? `\nTier: ${data.subscription_tier}` : '';
                    const creditLine = data.credit_amount !== undefined && data.credit_amount !== null ? `\n积分: ${data.credit_amount}` : '';
                    const msg = `✅ 检验成功！\n文件: ${filename}\nProject ID: ${data.project_id}${tierLine}${creditLine}\n\n${data.message}`;
                    showStatus(msg.replace(/\n/g, '<br>'), 'success');
                    await fetchCreds(type, true);
                } else {
                    showStatus(`❌ ${data.message || '检验失败'}`, 'error');
                }
            } catch (err) {
                showStatus(`❌ 检验失败: ${err.message}`, 'error');
            }
        };

        // 批量检验 Project ID
        const batchVerifyProjectIds = async (type) => {
            const m = getManager(type);
            if (m.selectedFiles.length === 0) {
                showStatus('❌ 请先选择要检验的凭证', 'error');
                return;
            }
            if (!await showConfirm(`确定要批量检验 ${m.selectedFiles.length} 个凭证的 Project ID 吗？`, '批量检验确认', { type: 'info' })) return;

            showStatus(`🔍 正在并行检验 ${m.selectedFiles.length} 个凭证...`, 'info');
            const promises = m.selectedFiles.map(async (filename) => {
                try {
                    const modeParam = type === 'antigravity' ? '?mode=antigravity' : '';
                    const res = await fetch(`./creds/verify-project/${encodeURIComponent(filename)}${modeParam}`, {
                        method: 'POST',
                        headers: getAuthHeaders()
                    });
                    const data = await res.json();
                    if (res.ok && data.success) {
                        return { success: true, filename, projectId: data.project_id, credit: data.credit_amount };
                    }
                    return { success: false, filename, error: data.message || '失败' };
                } catch (e) {
                    return { success: false, filename, error: e.message };
                }
            });

            const results = await Promise.all(promises);
            let successCount = 0;
            const resMsgs = [];
            results.forEach(r => {
                if (r.success) {
                    successCount++;
                    resMsgs.push(`✅ ${r.filename}: ${r.projectId}${r.credit !== undefined ? ` (积分: ${r.credit})` : ''}`);
                } else {
                    resMsgs.push(`❌ ${r.filename}: ${r.error}`);
                }
            });

            await fetchCreds(type, true);
            showStatus(`批量检验完成：成功 ${successCount}/${results.length} 个`, successCount > 0 ? 'success' : 'error');
        };

        // 冒烟测试
        const testCredential = async (type, filename) => {
            try {
                showStatus('🧪 正在测试凭证，请稍候...', 'info');
                const modeParam = type === 'antigravity' ? '?mode=antigravity' : '';
                const response = await fetch(`./creds/test/${encodeURIComponent(filename)}${modeParam}`, {
                    method: 'POST',
                    headers: getAuthHeaders()
                });
                const data = await response.json();
                if (response.status === 200 || response.status === 429 || data.success) {
                    const isRate = response.status === 429;
                    const icon = isRate ? '⚠️' : '✅';
                    const title = isRate ? '测试提示 (限流中)' : '测试成功';
                    showStatus(`${icon} ${data.message || '测试成功！'}`, isRate ? 'warning' : 'success');
                    await fetchCreds(type, true);
                } else {
                    showStatus(`❌ 测试失败 - ${data.message || ''}`, 'error');
                }
            } catch (err) {
                showStatus(`❌ 测试失败: ${err.message}`, 'error');
            }
        };

        // 批量冒烟测试
        const batchTestCredentials = async (type) => {
            const m = getManager(type);
            if (m.selectedFiles.length === 0) {
                showStatus('❌ 请先选择要测试的凭证', 'error');
                return;
            }
            if (!await showConfirm(`确定要批量测试 ${m.selectedFiles.length} 个凭证吗？`, '批量测试确认', { type: 'info' })) return;

            showStatus(`🧪 正在并行测试 ${m.selectedFiles.length} 个凭证...`, 'info');
            const promises = m.selectedFiles.map(async (filename) => {
                try {
                    const modeParam = type === 'antigravity' ? '?mode=antigravity' : '';
                    const res = await fetch(`./creds/test/${encodeURIComponent(filename)}${modeParam}`, {
                        method: 'POST',
                        headers: getAuthHeaders()
                    });
                    const data = await res.json();
                    if (res.status === 200 || res.status === 429 || data.success) {
                        return { success: true, filename, message: data.message || '成功' };
                    }
                    return { success: false, filename, error: data.message || data.error || '失败' };
                } catch (e) {
                    return { success: false, filename, error: e.message };
                }
            });

            const results = await Promise.all(promises);
            let successCount = 0;
            results.forEach(r => {
                if (r.success) successCount++;
            });

            await fetchCreds(type, true);
            showStatus(`批量测试完成：成功 ${successCount}/${results.length} 个`, successCount > 0 ? 'success' : 'error');
        };

        const toggleErrorDetails = async (type, filename, pathId) => {
            const m = getManager(type);
            if (m.expandedErrors[pathId]) {
                delete m.expandedErrors[pathId];
                return;
            }
            m.expandedErrors[pathId] = { loading: true };
            try {
                const modeParam = type === 'antigravity' ? 'mode=antigravity' : 'mode=geminicli';
                const res = await fetch(`./creds/errors/${encodeURIComponent(filename)}?${modeParam}`, { headers: getAuthHeaders() });
                const data = await res.json();
                if (res.ok) {
                    m.expandedErrors[pathId] = {
                        loading: false,
                        errorCodes: data.error_codes || [],
                        errorMessages: data.error_messages || {}
                    };
                } else {
                    m.expandedErrors[pathId] = { loading: false, error: data.detail || '未知错误' };
                }
            } catch (e) {
                m.expandedErrors[pathId] = { loading: false, error: e.message };
            }
        };

        const batchToggleErrorDetails = async (type) => {
            const m = getManager(type);
            if (m.selectedFiles.length === 0) {
                showStatus('❌ 请先选择要查看的凭证', 'error');
                return;
            }
            const allExpanded = m.selectedFiles.every(filename => {
                const item = m.items.find(i => i.filename === filename);
                return item && m.expandedErrors[item.pathId];
            });

            if (allExpanded) {
                m.selectedFiles.forEach(filename => {
                    const item = m.items.find(i => i.filename === filename);
                    if (item && m.expandedErrors[item.pathId]) {
                        delete m.expandedErrors[item.pathId];
                    }
                });
            } else {
                showStatus(`⏳ 正在加载 ${m.selectedFiles.length} 个报错信息...`, 'info');
                await Promise.all(m.selectedFiles.map(async (filename) => {
                    const item = m.items.find(i => i.filename === filename);
                    if (item && !m.expandedErrors[item.pathId]) {
                        await toggleErrorDetails(type, filename, item.pathId);
                    }
                }));
                showStatus(`已展开 ${m.selectedFiles.length} 个报错信息`, 'success');
            }
        };

        // 单个刷新额度
        const refreshSingleQuota = async (type, filename) => {
            showStatus(`正在刷新 ${filename} 的额度...`, 'info');
            try {
                const res = await fetch(`./creds/quota/${encodeURIComponent(filename)}?mode=antigravity`, { headers: getAuthHeaders() });
                const data = await res.json();
                if (res.ok && data.success) {
                    showStatus('额度信息刷新成功并已保存！', 'success');
                    await fetchCreds(type, true);
                } else {
                    showStatus(`刷新额度失败: ${data.error || '未知错误'}`, 'error');
                }
            } catch (e) {
                showStatus(`网络错误: ${e.message}`, 'error');
            }
        };

        // 批量刷新邮箱 / 额度
        const batchRefreshEmails = async (type) => {
            const m = getManager(type);
            if (m.selectedFiles.length === 0) return showStatus('❌ 请先选择凭证', 'error');
            if (!await showConfirm(`确定刷新 ${m.selectedFiles.length} 个凭证的邮箱吗？`, '刷新邮箱确认', { type: 'info' })) return;

            showStatus(`正在刷新 ${m.selectedFiles.length} 个凭证的邮箱...`, 'info');
            const promises = m.selectedFiles.map(async (filename) => {
                try {
                    const modeParam = type === 'antigravity' ? '?mode=antigravity' : '';
                    const res = await fetch(`./creds/fetch-email/${encodeURIComponent(filename)}${modeParam}`, {
                        method: 'POST',
                        headers: getAuthHeaders()
                    });
                    const data = await res.json();
                    return res.ok && data.user_email ? { success: true } : { success: false };
                } catch (e) {
                    return { success: false };
                }
            });

            const results = await Promise.all(promises);
            const successCount = results.filter(r => r.success).length;
            showStatus(`邮箱刷新完成：成功 ${successCount}/${m.selectedFiles.length} 个`, 'success');
            await fetchCreds(type, true);
        };

        const batchRefreshQuotas = async (type) => {
            const m = getManager(type);
            if (m.selectedFiles.length === 0) return showStatus('❌ 请先选择凭证', 'error');
            if (!await showConfirm(`确定刷新 ${m.selectedFiles.length} 个凭证的额度吗？`, '刷新额度确认', { type: 'info' })) return;

            showStatus(`正在刷新 ${m.selectedFiles.length} 个凭证的额度...`, 'info');
            const promises = m.selectedFiles.map(async (filename) => {
                try {
                    const res = await fetch(`./creds/quota/${encodeURIComponent(filename)}?mode=antigravity`, { headers: getAuthHeaders() });
                    const data = await res.json();
                    return res.ok && data.success ? { success: true } : { success: false };
                } catch (e) {
                    return { success: false };
                }
            });

            const results = await Promise.all(promises);
            const successCount = results.filter(r => r.success).length;
            showStatus(`额度刷新完成：成功 ${successCount}/${m.selectedFiles.length} 个`, 'success');
            await fetchCreds(type, true);
        };

        // OAuth 设置 Preview
        const batchConfigurePreview = async () => {
            if (oauth.selectedFiles.length === 0) return showStatus('❌ 请先选择要配置Preview的凭证', 'error');
            if (!await showConfirm(`确定要为 ${oauth.selectedFiles.length} 个凭证设置 Preview 通道吗？`, '配置 Preview 通道', { type: 'warning' })) return;

            showStatus(`🔧 正在为 ${oauth.selectedFiles.length} 个凭证配置 Preview 通道...`, 'info');
            const promises = oauth.selectedFiles.map(async (filename) => {
                try {
                    const res = await fetch(`./creds/configure-preview/${encodeURIComponent(filename)}`, { method: 'POST', headers: getAuthHeaders() });
                    const data = await res.json();
                    return res.ok && data.success ? { success: true, filename, msg: data.message } : { success: false, filename, error: data.message || '失败' };
                } catch (e) {
                    return { success: false, filename, error: e.message };
                }
            });
            const results = await Promise.all(promises);
            const successCount = results.filter(r => r.success).length;
            showStatus(`Preview 通道配置完成：成功 ${successCount}/${oauth.selectedFiles.length} 个`, successCount > 0 ? 'success' : 'error');
            await fetchCreds('oauth', true);
        };

        let isCompletingOAuth = false;
        const autoCompleteOAuth = async (type) => {
            if (isCompletingOAuth) return;
            isCompletingOAuth = true;
            try {
                const mode = type === 'antigravity' ? 'antigravity' : 'geminicli';
                const res = await fetch(`./auth/complete?mode=${mode}`, {
                    method: 'POST',
                    headers: getAuthHeaders()
                });
                const data = await res.json();
                if (res.ok && data.success) {
                    showStatus(data.message || '凭证获取并保存成功！', 'success');
                    const m = getManager(type);
                    m.authPanelVisible = false;
                    fetchCreds(type, true);
                }
            } catch (e) {
                console.error("Auto complete OAuth error:", e);
            } finally {
                isCompletingOAuth = false;
            }
        };

        // OAuth 授权获取逻辑
        const startAuth = async (type) => {
            const m = getManager(type);
            m.authPanelVisible = true;
            m.authUrl = '';
            try {
                showStatus('正在生成 OAuth 授权链接...', 'info');
                const mode = type === 'antigravity' ? 'antigravity' : 'geminicli';
                const payload = {
                    mode: mode,
                    project_id: m.projectIdInput || null
                };
                const res = await fetch('./auth/start', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        ...getAuthHeaders()
                    },
                    body: JSON.stringify(payload)
                });
                const data = await res.json();
                if (res.ok && data.auth_url) {
                    m.authUrl = data.auth_url;
                    showStatus('OAuth 授权链接生成成功', 'success');
                    // 完全照抄 AntigravityScheduler: 自动启动后台轮询/等待
                    autoCompleteOAuth(type);
                } else {
                    const errorMsg = data.detail || data.error || '未知错误';
                    showStatus(`生成失败: ${errorMsg}`, 'error');
                }
            } catch (e) {
                showStatus(`网络错误: ${e.message}`, 'error');
            }
        };

        const processCallbackUrl = async (type) => {
            const m = getManager(type);
            if (!m.callbackUrlInput) return showStatus('请先粘贴回调 URL', 'error');

            try {
                showStatus('正在验证回调 URL 并提取凭证...', 'info');
                const mode = type === 'antigravity' ? 'antigravity' : 'geminicli';
                const payload = {
                    callback_url: m.callbackUrlInput,
                    mode: mode
                };
                const res = await fetch('./auth/callback-url', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        ...getAuthHeaders()
                    },
                    body: JSON.stringify(payload)
                });
                const data = await res.json();
                if (res.ok) {
                    showStatus(data.message || '凭证获取成功！', 'success');
                    m.authPanelVisible = false;
                    m.callbackUrlInput = '';
                    await fetchCreds(type, true);
                } else {
                    const errorMsg = data.detail || data.error || '未知错误';
                    showStatus(`获取凭证失败: ${errorMsg}`, 'error');
                }
            } catch (e) {
                showStatus(`处理失败: ${e.message}`, 'error');
            }
        };

        // 跨窗口消息监听（对标 AntigravityScheduler 授权窗口自动通知本主窗口）
        window.addEventListener('message', (e) => {
            if (e.data && e.data.type === 'oauth-success') {
                showStatus('接收到浏览器窗口授权成功广播，凭证已自动获取成功！', 'success');
                if (ag.authPanelVisible) ag.authPanelVisible = false;
                if (oauth.authPanelVisible) oauth.authPanelVisible = false;
                fetchCreds('antigravity', true);
                fetchCreds('oauth', true);
            }
        });

        // 手动点击【获取认证凭证】按钮 (完全照抄对标 AntigravityScheduler completeOAuthFlow 机制)
        const completeOAuthFlow = async (type) => {
            const m = getManager(type);
            const btnId = type === 'antigravity' ? 'getCredsBtn' : 'getCredsBtnOAuth';
            const btn = document.getElementById(btnId);
            if (btn) {
                btn.disabled = true;
                btn.textContent = '正在获取凭证...';
            }
            showStatus('正在向服务器查询并获取认证凭证...', 'info');
            try {
                const mode = type === 'antigravity' ? 'antigravity' : 'geminicli';
                const res = await fetch(`./auth/complete?mode=${mode}`, {
                    method: 'POST',
                    headers: getAuthHeaders()
                });
                const data = await res.json();
                if (res.ok && data.success) {
                    const msg = data.message || '账号凭证添加成功！';
                    showStatus(msg, 'success');
                    alert(msg);
                    m.authPanelVisible = false;
                    fetchCreds(type, true);
                } else {
                    const errMsg = data.error || data.detail || '未检测到授权回调，请确保已在浏览器中完成授权。';
                    showStatus(errMsg, 'warning');
                    alert(errMsg);
                }
            } catch (e) {
                const errText = `获取凭证异常: ${e.message}`;
                showStatus(errText, 'error');
                alert(errText);
            } finally {
                if (btn) {
                    btn.disabled = false;
                    btn.textContent = '获取认证凭证';
                }
            }
        };

        const downloadAllCreds = (type) => {
            const modeParam = type === 'antigravity' ? 'mode=antigravity' : 'mode=geminicli';
            window.location.href = `./creds/download-all?${modeParam}&token=${encodeURIComponent(token.value)}`;
        };

        const getGeminiQuotaGroups = (quotaGroups) => {
            if (!quotaGroups || !Array.isArray(quotaGroups)) return [];
            const filtered = quotaGroups.filter(g => (g.displayName || '').toUpperCase().includes('GEMINI'));
            return filtered.length > 0 ? filtered : quotaGroups;
        };

        const getQuotaBuckets = (quotaGroups) => {
            if (!quotaGroups || !Array.isArray(quotaGroups)) return [];
            let targetGroups = quotaGroups.filter(g => (g.displayName || '').toUpperCase().includes('GEMINI'));
            if (targetGroups.length === 0) {
                targetGroups = quotaGroups;
            }
            const buckets = [];
            targetGroups.forEach(g => {
                if (g.buckets && Array.isArray(g.buckets)) {
                    g.buckets.forEach(b => buckets.push(b));
                }
            });
            return buckets;
        };

        const getBucketRemainingPercent = (bucket) => {
            if (!bucket) return 0;
            const frac = bucket.remainingFraction !== undefined ? bucket.remainingFraction : 1.0;
            return Math.min(100, Math.max(0, Math.round(frac * 100)));
        };

        const getBucketPercent = (bucket) => {
            if (!bucket) return 0;
            const rem = getBucketRemainingPercent(bucket);
            return Math.min(100, Math.max(0, 100 - rem));
        };

        const getBucketLabel = (bucket) => {
            if (!bucket) return '';
            let rawLabel = bucket.displayName || bucket.window || '配额';
            return rawLabel
                .replace(/Weekly Limit/gi, '周限额')
                .replace(/Five Hour Limit/gi, '五小时限额');
        };

        const getBucketTitle = (bucket) => {
            if (!bucket) return '';
            const label = getBucketLabel(bucket);
            const used = getBucketPercent(bucket);
            const rem = getBucketRemainingPercent(bucket);
            const reset = bucket.resetTime ? ` (刷新重置: ${bucket.resetTime})` : '';
            return `${label}: 已使用 ${used}% (剩余 ${rem}%)${reset}`;
        };

        const getBucketResetTime = (bucket) => {
            if (!bucket) return '';
            let targetDate = null;

            if (bucket.resetTimeRaw) {
                try {
                    targetDate = new Date(bucket.resetTimeRaw);
                } catch (e) {}
            }

            if ((!targetDate || isNaN(targetDate.getTime())) && bucket.resetTime && bucket.resetTime !== 'N/A') {
                try {
                    const parts = bucket.resetTime.trim().split(' ');
                    if (parts.length === 2) {
                        const dateParts = parts[0].split('-');
                        const timeParts = parts[1].split(':');
                        if (dateParts.length === 2 && timeParts.length === 2) {
                            const now = new Date();
                            const month = parseInt(dateParts[0], 10) - 1;
                            const day = parseInt(dateParts[1], 10);
                            const hour = parseInt(timeParts[0], 10);
                            const min = parseInt(timeParts[1], 10);
                            targetDate = new Date(now.getFullYear(), month, day, hour, min);
                            if (targetDate.getTime() < now.getTime() - 180 * 86400 * 1000) {
                                targetDate.setFullYear(now.getFullYear() + 1);
                            }
                        }
                    }
                } catch (e) {}
            }

            if (!targetDate || isNaN(targetDate.getTime())) {
                return bucket.resetTime && bucket.resetTime !== 'N/A' ? bucket.resetTime : '';
            }

            const diffMs = targetDate.getTime() - Date.now();
            if (diffMs <= 0) return '即将';

            const totalSec = Math.floor(diffMs / 1000);
            const days = Math.floor(totalSec / 86400);
            const hours = Math.floor((totalSec % 86400) / 3600);
            const mins = Math.floor((totalSec % 3600) / 60);

            if (days > 0) {
                return `${days}天${hours}小时后`;
            } else if (hours > 0) {
                return `${hours}小时${mins}分后`;
            } else if (mins > 0) {
                return `${mins}分钟后`;
            } else {
                return `不到1分钟后`;
            }
        };

        const getBucketIcon = (bucket) => {
            return '📊';
        };

        const getBucketColor = (bucket) => {
            const used = getBucketPercent(bucket);
            if (used < 50) return 'linear-gradient(90deg, #10b981, #059669)';
            if (used < 80) return 'linear-gradient(90deg, #f59e0b, #d97706)';
            return 'linear-gradient(90deg, #ef4444, #dc2626)';
        };

        const calculateQuotaPercent = (group) => {
            if (!group) return 0;
            if (group.buckets && Array.isArray(group.buckets) && group.buckets.length > 0) {
                const fractions = group.buckets.map(b => b.remainingFraction !== undefined ? b.remainingFraction : 1.0);
                const minFraction = Math.min(...fractions);
                return Math.min(100, Math.max(0, Math.round(minFraction * 100)));
            }
            const current = group.currentQuota !== undefined ? group.currentQuota : (group.quota || 0);
            const total = group.totalQuota || group.limit || 100;
            if (total <= 0) return 0;
            return Math.min(100, Math.max(0, Math.round((current / total) * 100)));
        };

        const getQuotaRemainingText = (group) => {
            if (!group) return 'N/A';
            if (group.buckets && Array.isArray(group.buckets) && group.buckets.length > 0) {
                return group.buckets.map(b => {
                    const pct = Math.round((b.remainingFraction !== undefined ? b.remainingFraction : 1.0) * 100);
                    const reset = b.resetTime ? ` (重置时间: ${b.resetTime})` : '';
                    const label = b.displayName || b.window || '通用配额';
                    return `${label}: 剩余 ${pct}%${reset}`;
                }).join(' ; ');
            }
            const current = group.currentQuota !== undefined ? group.currentQuota : (group.quota || 0);
            const total = group.totalQuota || group.limit || 100;
            return `${current}/${total}`;
        };

        const getActiveCooldowns = (cooldowns) => {
            if (!cooldowns) return {};
            const now = Date.now() / 1000;
            const res = {};
            for (const [m, until] of Object.entries(cooldowns)) {
                if (until > now) res[m] = until;
            }
            return res;
        };

        const formatCooldownBadge = (modelName, until) => {
            const remaining = Math.max(0, Math.floor(until - Date.now() / 1000));
            const shortModel = modelName.replace('gemini-', '').replace('-exp', '').replace('2.0-', '2-').replace('1.5-', '1.5-');
            const m = Math.floor(remaining / 60);
            const s = remaining % 60;
            const timeStr = m > 0 ? `${m}m${s}s` : `${s}s`;
            return `${shortModel}: ${timeStr}`;
        };

        const parseBucketResetDate = (bucket) => {
            if (!bucket) return null;
            if (bucket.resetTimeRaw) {
                try {
                    const d = new Date(bucket.resetTimeRaw);
                    if (!isNaN(d.getTime())) return d;
                } catch (e) {}
            }
            if (bucket.resetTime && bucket.resetTime !== 'N/A') {
                try {
                    const parts = bucket.resetTime.trim().split(' ');
                    if (parts.length === 2) {
                        const dateParts = parts[0].split('-');
                        const timeParts = parts[1].split(':');
                        if (dateParts.length === 2 && timeParts.length === 2) {
                            const now = new Date();
                            const month = parseInt(dateParts[0], 10) - 1;
                            const day = parseInt(dateParts[1], 10);
                            const hour = parseInt(timeParts[0], 10);
                            const min = parseInt(timeParts[1], 10);
                            let targetDate = new Date(now.getFullYear(), month, day, hour, min);
                            if (targetDate.getTime() < now.getTime() - 180 * 86400 * 1000) {
                                targetDate.setFullYear(now.getFullYear() + 1);
                            }
                            if (!isNaN(targetDate.getTime())) return targetDate;
                        }
                    }
                } catch (e) {}
            }
            return null;
        };

        const classifyAccountBuckets = (item) => {
            const buckets = getQuotaBuckets(item.quota_groups);
            let weeklyBucket = null;
            let dailyBucket = null;

            if (buckets && buckets.length > 0) {
                buckets.forEach(b => {
                    const name = (b.displayName || b.window || b.bucketId || '').toLowerCase();
                    if (name.includes('week') || name.includes('周')) {
                        weeklyBucket = b;
                    } else if (name.includes('day') || name.includes('daily') || name.includes('hour') || name.includes('five') || name.includes('日') || name.includes('小时')) {
                        dailyBucket = b;
                    }
                });

                if (!weeklyBucket && !dailyBucket) {
                    if (buckets.length === 1) {
                        weeklyBucket = buckets[0];
                    } else {
                        weeklyBucket = buckets[0];
                        dailyBucket = buckets[1];
                    }
                } else if (!weeklyBucket && dailyBucket) {
                    weeklyBucket = buckets.find(b => b !== dailyBucket) || dailyBucket;
                } else if (weeklyBucket && !dailyBucket) {
                    dailyBucket = buckets.find(b => b !== weeklyBucket) || null;
                }
            }

            return { weeklyBucket, dailyBucket };
        };

        const getDailyQuotaUsedAvgNum = (type) => {
            const m = getManager(type);
            const activeItems = (m.items || []).filter(item => !item.disabled);
            if (activeItems.length === 0) return 0;

            let sumDailyUsedFrac = 0;
            activeItems.forEach(item => {
                const { weeklyBucket, dailyBucket } = classifyAccountBuckets(item);
                const targetBucket = dailyBucket || weeklyBucket;
                const frac = targetBucket && targetBucket.remainingFraction !== undefined ? targetBucket.remainingFraction : 1.0;
                sumDailyUsedFrac += (1.0 - frac);
            });

            const avgDailyUsedPercent = (sumDailyUsedFrac / activeItems.length) * 100;
            return Number(avgDailyUsedPercent.toFixed(1));
        };

        const getWeeklyQuotaUsedAvgNum = (type) => {
            const m = getManager(type);
            const activeItems = (m.items || []).filter(item => !item.disabled);
            if (activeItems.length === 0) return 0;

            let sumWeeklyUsedFrac = 0;
            activeItems.forEach(item => {
                const { weeklyBucket } = classifyAccountBuckets(item);
                const frac = weeklyBucket && weeklyBucket.remainingFraction !== undefined ? weeklyBucket.remainingFraction : 1.0;
                sumWeeklyUsedFrac += (1.0 - frac);
            });

            const avgWeeklyUsedPercent = (sumWeeklyUsedFrac / activeItems.length) * 100;
            return Number(avgWeeklyUsedPercent.toFixed(1));
        };

        const getDailyQuotaAvg = (type) => {
            const used = getDailyQuotaUsedAvgNum(type);
            return (100 - used).toFixed(1) + '%';
        };

        const getWeeklyQuotaAvg = (type) => {
            const used = getWeeklyQuotaUsedAvgNum(type);
            return (100 - used).toFixed(1) + '%';
        };

        const getWeeklyQuotaUsedAvg = (type) => {
            return getWeeklyQuotaUsedAvgNum(type).toFixed(1) + '%';
        };

        const getDailyQuotaUsedAvg = (type) => {
            return getDailyQuotaUsedAvgNum(type).toFixed(1) + '%';
        };

        const totalPages = (type) => {
            const m = getManager(type);
            return Math.ceil(m.total / m.pageSize) || 1;
        };

        const changePage = (type, delta) => {
            const m = getManager(type);
            const newPage = m.currentPage + delta;
            if (newPage >= 1 && newPage <= totalPages(type)) {
                m.currentPage = newPage;
                fetchCreds(type);
            }
        };

        // URL 复制助手
        const cpUrl = (urlText) => {
            navigator.clipboard.writeText(urlText).then(() => {
                showStatus(`已复制 API 地址: ${urlText}`, 'success');
            }).catch(() => {
                showStatus('复制失败，请手动复制', 'error');
            });
        };

        const getBaseUrl = () => `${window.location.protocol}//${window.location.host}`;

        const cpAllUrls = (type) => {
            const base = getBaseUrl();
            const prefix = type === 'antigravity' ? 'antigravity/' : '';
            const allText = `OpenAI: ${base}/${prefix}v1/chat/completions\nClaude: ${base}/${prefix}v1/messages\nGemini: ${base}/${prefix}v1beta/models`;
            navigator.clipboard.writeText(allText).then(() => {
                showStatus('已成功一键复制所有格式 API 地址！', 'success');
            });
        };

        // ----------------------------------------------------------------------
        // 3. 批量上传 Tab 逻辑 (`upload`)
        // ----------------------------------------------------------------------
        const upload = reactive({
            gcpFiles: [],
            gcpProgress: 0,
            gcpUploading: false,
            agFiles: [],
            agProgress: 0,
            agUploading: false
        });

        const handleFileSelect = (event, type) => {
            const selected = Array.from(event.target.files || []);
            if (type === 'gcp') {
                upload.gcpFiles = [...upload.gcpFiles, ...selected];
            } else {
                upload.agFiles = [...upload.agFiles, ...selected];
            }
        };

        const handleFileDrop = (event, type) => {
            event.preventDefault();
            const dropped = Array.from(event.dataTransfer.files || []);
            if (type === 'gcp') {
                upload.gcpFiles = [...upload.gcpFiles, ...dropped];
            } else {
                upload.agFiles = [...upload.agFiles, ...dropped];
            }
        };

        const doUploadFiles = async (type) => {
            const files = type === 'gcp' ? upload.gcpFiles : upload.agFiles;
            if (files.length === 0) return showStatus('请先选择要上传的文件', 'error');

            const isGcp = type === 'gcp';
            if (isGcp) upload.gcpUploading = true; else upload.agUploading = true;

            const formData = new FormData();
            files.forEach(f => formData.append('files', f));

            try {
                const modeParam = type === 'antigravity' ? '?mode=antigravity' : '?mode=geminicli';
                const res = await fetch(`./auth/upload-creds-batch${modeParam}`, {
                    method: 'POST',
                    headers: { 'Authorization': `Bearer ${token.value}` },
                    body: formData
                });
                const data = await res.json();
                if (res.ok) {
                    showStatus(`上传成功！共解析导入 ${data.imported_count || data.count || files.length} 个凭证`, 'success');
                    if (isGcp) upload.gcpFiles = []; else upload.agFiles = [];
                    await fetchCreds(type, true);
                } else {
                    showStatus(`上传失败: ${data.detail || '未知错误'}`, 'error');
                }
            } catch (e) {
                showStatus(`上传网络错误: ${e.message}`, 'error');
            } finally {
                if (isGcp) upload.gcpUploading = false; else upload.agUploading = false;
            }
        };

        // ----------------------------------------------------------------------
        // 4. 模型映射 Tab 逻辑 (`models`)
        // ----------------------------------------------------------------------
        const models = reactive({
            mappings: {},
            dynamicMappings: [],
            fallbackModel: '',
            availableOptions: [
                'gemini-2.5-flash',
                'gemini-2.5-pro',
                'gemini-2.5-flash-lite',
                'gemini-2.5-flash-thinking',
                'gemini-3.5-flash',
                'gemini-3.5-flash-low',
                'gemini-3.5-flash-extra-low',
                'gemini-3.1-pro-low',
                'gemini-3.1-flash-lite',
                'gemini-3-flash',
                'gemini-3-flash-agent',
                'gemini-pro-agent',
                'claude-sonnet-4-6',
                'claude-opus-4-6-thinking'
            ],
            newOriginal: '',
            newTarget: '',
            loading: false
        });

        const loadAvailableModelOptions = async () => {
            try {
                const res = await fetch('./antigravity/v1/models', { headers: getAuthHeaders() });
                if (res.ok) {
                    const data = await res.json();
                    if (data.data && Array.isArray(data.data)) {
                        const set = new Set(models.availableOptions);
                        data.data.forEach(item => {
                            if (item && item.id) {
                                let id = item.id;
                                if (id.startsWith('假流式/')) id = id.replace('假流式/', '');
                                if (id.startsWith('流式抗截断/')) id = id.replace('流式抗截断/', '');
                                set.add(id);
                            }
                        });
                        models.availableOptions = Array.from(set);
                    }
                }
            } catch (e) {
                console.error('动态拉取模型可用列表失败', e);
            }
        };

        const loadModelMappings = async () => {
            models.loading = true;
            try {
                const res = await fetch('./model-mappings', { headers: getAuthHeaders() });
                const data = await res.json();
                if (res.ok && data.data) {
                    const mapObj = {};
                    const customList = data.data.custom_mappings || [];
                    customList.forEach(item => {
                        if (item.requested_model && item.target_model) {
                            mapObj[item.requested_model] = item.target_model;
                        }
                    });
                    models.mappings = mapObj;
                    models.dynamicMappings = data.data.dynamic_mappings || [];
                    const fbMap = data.data.fallback_mappings || data.data.fallback_map || {};
                    models.fallbackModel = fbMap.antigravity || fbMap.default || '';
                }
                await loadAvailableModelOptions();
            } catch (e) {
                showStatus(`加载模型映射失败: ${e.message}`, 'error');
            } finally {
                models.loading = false;
            }
        };

        const saveFallbackModel = async () => {
            try {
                showStatus('正在保存全局兜底模型...', 'info');
                const res = await fetch('./model-mappings/set-fallback', {
                    method: 'POST',
                    headers: getAuthHeaders(),
                    body: JSON.stringify({
                        fallback_model: models.fallbackModel,
                        router_type: 'antigravity'
                    })
                });
                const data = await res.json();
                if (res.ok) {
                    showStatus('✅ 全局兜底模型设置保存成功！', 'success');
                    await loadModelMappings();
                } else {
                    showStatus(`保存失败: ${data.detail || '未知错误'}`, 'error');
                }
            } catch (e) {
                showStatus(`保存失败: ${e.message}`, 'error');
            }
        };

        const addModelMapping = async () => {
            if (!models.newOriginal || !models.newTarget) return showStatus('请输入完整映射模型名称', 'error');
            try {
                const res = await fetch('./model-mappings', {
                    method: 'POST',
                    headers: getAuthHeaders(),
                    body: JSON.stringify({
                        requested_model: models.newOriginal.trim(),
                        target_model: models.newTarget.trim(),
                        router_type: 'antigravity'
                    })
                });
                if (res.ok) {
                    showStatus('添加模型映射成功！', 'success');
                    models.newOriginal = '';
                    models.newTarget = '';
                    await loadModelMappings();
                } else {
                    const errData = await res.json().catch(() => ({}));
                    showStatus(`添加失败: ${errData.detail || '接口错误'}`, 'error');
                }
            } catch (e) {
                showStatus(`添加失败: ${e.message}`, 'error');
            }
        };

        const deleteModelMapping = async (orig) => {
            try {
                const res = await fetch('./model-mappings', {
                    method: 'DELETE',
                    headers: getAuthHeaders(),
                    body: JSON.stringify({
                        requested_model: orig,
                        router_type: 'antigravity'
                    })
                });
                if (res.ok) {
                    showStatus(`已删除 ${orig} 映射`, 'success');
                    await loadModelMappings();
                } else {
                    showStatus('删除失败', 'error');
                }
            } catch (e) {
                showStatus(`删除失败: ${e.message}`, 'error');
            }
        };

        const clearModelMappings = async () => {
            if (!await showConfirm('确定清空所有自定义模型映射记录吗？', '清空确认', { type: 'danger', confirmText: '确认清空' })) return;
            try {
                const res = await fetch('./model-mappings/clear-custom', {
                    method: 'POST',
                    headers: getAuthHeaders()
                });
                if (res.ok) {
                    showStatus('已清空模型映射记录', 'success');
                    await loadModelMappings();
                } else {
                    showStatus('清空失败', 'error');
                }
            } catch (e) {
                showStatus(`清空失败: ${e.message}`, 'error');
            }
        };

        const clearDynamicMappings = async () => {
            if (!await showConfirm('确定清空所有实时抓取的动态模型映射记录吗？', '清空确认', { type: 'danger', confirmText: '确认清空' })) return;
            try {
                const res = await fetch('./model-mappings/clear-dynamic', {
                    method: 'POST',
                    headers: getAuthHeaders()
                });
                if (res.ok) {
                    showStatus('已清空动态映射记录', 'success');
                    await loadModelMappings();
                } else {
                    showStatus('清空失败', 'error');
                }
            } catch (e) {
                showStatus(`清空失败: ${e.message}`, 'error');
            }
        };

        // ----------------------------------------------------------------------
        // 4.5. Token 看板 Tab 逻辑 (`tokens`)
        // ----------------------------------------------------------------------
        const tokenDashboard = reactive({
            loading: false,
            trendPeriod: 'daily', // 'daily', 'weekly', 'monthly'
            summary: {
                total_tokens: 0,
                prompt_tokens: 0,
                completion_tokens: 0,
                total_requests: 0,
                today_tokens: 0,
                this_week_tokens: 0,
                this_month_tokens: 0
            },
            accountRanking: [],
            modelRanking: [],
            trend: {
                daily: [],
                weekly: [],
                monthly: []
            }
        });

        const loadTokenDashboard = async () => {
            tokenDashboard.loading = true;
            try {
                let res = await fetch('./token-dashboard/stats', { headers: getAuthHeaders() });
                if (!res.ok) {
                    res = await fetch('./token-dashboard', { headers: getAuthHeaders() });
                }
                const data = await res.json();
                if (res.ok && data.success) {
                    const stats = data.data || {};
                    tokenDashboard.summary = stats.summary || {};
                    tokenDashboard.accountRanking = stats.account_ranking || stats.accountRanking || [];
                    tokenDashboard.modelRanking = stats.model_ranking || stats.modelRanking || [];
                    tokenDashboard.trend = stats.trend || { daily: [], weekly: [], monthly: [] };
                } else {
                    showStatus(`加载 Token 看板数据失败: ${data.detail || '未知错误'}`, 'error');
                }
            } catch (e) {
                showStatus(`网络错误无法加载 Token 看板: ${e.message}`, 'error');
            } finally {
                tokenDashboard.loading = false;
            }
        };

        const clearTokenDashboard = async () => {
            if (!await showConfirm('确定要清空所有 Token 消耗历史记录吗？此操作无法撤销。', '清空 Token 历史', { type: 'danger', confirmText: '确认清空' })) return;
            try {
                let res = await fetch('./token-dashboard/clear', { method: 'POST', headers: getAuthHeaders() });
                const data = await res.json();
                if (res.ok && data.success) {
                    showStatus('已成功清空 Token 统计历史记录', 'success');
                    await loadTokenDashboard();
                } else {
                    showStatus(`清空失败: ${data.detail || '未知错误'}`, 'error');
                }
            } catch (e) {
                showStatus(`网络错误: ${e.message}`, 'error');
            }
        };

        const formatTokenCount = (num) => {
            if (num === null || num === undefined) return '0';
            num = Number(num);
            if (num >= 1000000) return (num / 1000000).toFixed(2) + ' M';
            if (num >= 1000) return (num / 1000).toFixed(1) + ' K';
            return num.toLocaleString();
        };

        const activeTrendList = computed(() => {
            const trend = tokenDashboard.trend || {};
            return trend[tokenDashboard.trendPeriod] || [];
        });

        const maxAccountTokens = computed(() => {
            const list = tokenDashboard.accountRanking || [];
            if (!list.length) return 1;
            const maxVal = Math.max(...list.map(item => item.total_tokens || 0));
            return maxVal > 0 ? maxVal : 1;
        });

        const maxModelTokens = computed(() => {
            const list = tokenDashboard.modelRanking || [];
            if (!list.length) return 1;
            const maxVal = Math.max(...list.map(item => item.total_tokens || 0));
            return maxVal > 0 ? maxVal : 1;
        });

        // ----------------------------------------------------------------------
        // Chart.js 折线图渲染与更新逻辑
        // ----------------------------------------------------------------------
        let tokenTrendChart = null;

        const renderTokenTrendChart = () => {
            if (typeof Chart === 'undefined') return;
            const canvas = document.getElementById('tokenTrendChartCanvas');
            if (!canvas) return;

            const list = activeTrendList.value;
            const labels = list.map(item => (item.date || item.week || item.month || '').slice(-5));
            const promptData = list.map(item => item.prompt_tokens || 0);
            const completionData = list.map(item => item.completion_tokens || 0);
            const totalData = list.map(item => item.total_tokens || 0);
            const cachedData = list.map(item => item.cached_tokens || 0);
            const thoughtsData = list.map(item => item.thoughts_tokens || 0);
            const requestsData = list.map(item => item.request_count || 0);

            const isDark = theme.value === 'dark';
            const gridColor = isDark ? 'rgba(255, 255, 255, 0.08)' : 'rgba(0, 0, 0, 0.06)';
            const textColor = isDark ? '#94a3b8' : '#64748b';

            if (tokenTrendChart) {
                tokenTrendChart.data.labels = labels;
                tokenTrendChart.data.datasets[0].data = promptData;
                tokenTrendChart.data.datasets[1].data = completionData;
                tokenTrendChart.data.datasets[2].data = cachedData;
                tokenTrendChart.data.datasets[3].data = thoughtsData;
                tokenTrendChart.data.datasets[4].data = totalData;
                tokenTrendChart.data.datasets[5].data = requestsData;
                tokenTrendChart.options.scales.x.ticks.color = textColor;
                tokenTrendChart.options.scales.x.grid.color = gridColor;
                tokenTrendChart.options.scales.y.ticks.color = textColor;
                tokenTrendChart.options.scales.y.grid.color = gridColor;
                tokenTrendChart.options.scales.y1.ticks.color = textColor;
                tokenTrendChart.options.plugins.legend.labels.color = textColor;
                tokenTrendChart.update();
                return;
            }

            const ctx = canvas.getContext('2d');
            const totalGradient = ctx.createLinearGradient(0, 0, 0, 200);
            totalGradient.addColorStop(0, 'rgba(139, 92, 246, 0.15)');
            totalGradient.addColorStop(1, 'rgba(139, 92, 246, 0.0)');

            tokenTrendChart = new Chart(ctx, {
                type: 'line',
                data: {
                    labels,
                    datasets: [
                        {
                            label: '📥 输入',
                            data: promptData,
                            borderColor: '#2563eb',
                            borderWidth: 1.8,
                            pointRadius: 3,
                            pointHoverRadius: 5,
                            pointBackgroundColor: '#ffffff',
                            pointBorderColor: '#2563eb',
                            pointBorderWidth: 1.5,
                            fill: false,
                            tension: 0.3,
                            yAxisID: 'y'
                        },
                        {
                            label: '📤 输出',
                            data: completionData,
                            borderColor: '#10b981',
                            borderWidth: 1.8,
                            pointRadius: 3,
                            pointHoverRadius: 5,
                            pointBackgroundColor: '#ffffff',
                            pointBorderColor: '#10b981',
                            pointBorderWidth: 1.5,
                            fill: false,
                            tension: 0.3,
                            yAxisID: 'y'
                        },
                        {
                            label: '⚡ 缓存',
                            data: cachedData,
                            borderColor: '#06b6d4',
                            borderWidth: 1.5,
                            borderDash: [4, 2],
                            pointRadius: 2.5,
                            pointHoverRadius: 4,
                            pointBackgroundColor: '#ffffff',
                            pointBorderColor: '#06b6d4',
                            pointBorderWidth: 1,
                            fill: false,
                            tension: 0.3,
                            yAxisID: 'y'
                        },
                        {
                            label: '🧠 思考',
                            data: thoughtsData,
                            borderColor: '#f59e0b',
                            borderWidth: 1.5,
                            borderDash: [2, 2],
                            pointRadius: 2.5,
                            pointHoverRadius: 4,
                            pointBackgroundColor: '#ffffff',
                            pointBorderColor: '#f59e0b',
                            pointBorderWidth: 1,
                            fill: false,
                            tension: 0.3,
                            yAxisID: 'y'
                        },
                        {
                            label: '💎 总 Token',
                            data: totalData,
                            borderColor: '#8b5cf6',
                            backgroundColor: totalGradient,
                            borderWidth: 2.2,
                            pointRadius: 3.5,
                            pointHoverRadius: 6,
                            pointBackgroundColor: '#ffffff',
                            pointBorderColor: '#8b5cf6',
                            pointBorderWidth: 2,
                            fill: true,
                            tension: 0.3,
                            yAxisID: 'y'
                        },
                        {
                            label: '🚀 API 请求数',
                            data: requestsData,
                            borderColor: '#ec4899',
                            borderWidth: 1.8,
                            pointRadius: 3,
                            pointHoverRadius: 5,
                            pointBackgroundColor: '#ffffff',
                            pointBorderColor: '#ec4899',
                            pointBorderWidth: 1.5,
                            fill: false,
                            tension: 0.3,
                            yAxisID: 'y1'
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: {
                        mode: 'index',
                        intersect: false
                    },
                    plugins: {
                        legend: {
                            position: 'top',
                            align: 'end',
                            labels: {
                                boxWidth: 12,
                                boxHeight: 2,
                                usePointStyle: false,
                                color: textColor,
                                font: { size: 11, weight: '500' }
                            }
                        },
                        tooltip: {
                            backgroundColor: '#0f172a',
                            titleColor: '#93c5fd',
                            bodyColor: '#ffffff',
                            padding: 10,
                            cornerRadius: 8,
                            callbacks: {
                                label: function(context) {
                                    if (context.dataset.yAxisID === 'y1') {
                                        return ` ${context.dataset.label}: ${context.raw.toLocaleString()} 次`;
                                    }
                                    return ` ${context.dataset.label}: ${formatTokenCount(context.raw)}`;
                                }
                            }
                        }
                    },
                    scales: {
                        x: {
                            grid: { color: gridColor, drawBorder: false },
                            ticks: { color: textColor, font: { size: 11 } }
                        },
                        y: {
                            type: 'linear',
                            display: true,
                            position: 'left',
                            beginAtZero: true,
                            grid: { color: gridColor, drawBorder: false },
                            ticks: {
                                color: textColor,
                                font: { size: 11 },
                                callback: function(val) {
                                    return formatTokenCount(val);
                                }
                            }
                        },
                        y1: {
                            type: 'linear',
                            display: true,
                            position: 'right',
                            beginAtZero: true,
                            grid: { drawOnChartArea: false },
                            ticks: {
                                color: textColor,
                                font: { size: 11 },
                                callback: function(val) {
                                    return val + ' 次';
                                }
                            }
                        }
                    }
                }
            });
        };

        watch([activeTrendList, theme], () => {
            nextTick(() => {
                renderTokenTrendChart();
            });
        });

        // ----------------------------------------------------------------------
        // 5. 系统配置 Tab 逻辑 (`config`)
        // ----------------------------------------------------------------------
        const config = reactive({
            form: {},
            envLocked: [],
            loading: false
        });

        const loadConfig = async () => {
            config.loading = true;
            try {
                const res = await fetch('./config', { headers: getAuthHeaders() });
                const data = await res.json();
                if (res.ok) {
                    config.form = data.config || {};
                    config.envLocked = data.env_locked || [];
                    showStatus('配置加载成功', 'success');
                }
            } catch (e) {
                showStatus(`加载配置失败: ${e.message}`, 'error');
            } finally {
                config.loading = false;
            }
        };

        const saveConfig = async () => {
            try {
                showStatus('正在保存全局配置...', 'info');
                const res = await fetch('./config', {
                    method: 'POST',
                    headers: getAuthHeaders(),
                    body: JSON.stringify({ config: config.form })
                });
                const data = await res.json();
                if (res.ok) {
                    showStatus('✅ 全局配置保存成功（热更新已同步生效）', 'success');
                    await loadConfig();
                } else {
                    showStatus(`保存失败: ${data.detail || '未知错误'}`, 'error');
                }
            } catch (e) {
                showStatus(`保存网络错误: ${e.message}`, 'error');
            }
        };

        const useMirrorUrls = () => {
            config.form.code_assist_endpoint = 'https://daily-cloudcode-pa.googleapis.com';
            config.form.oauth_proxy_url = 'https://daily-cloudcode-pa.googleapis.com';
            config.form.googleapis_proxy_url = 'https://daily-cloudcode-pa.googleapis.com';
            config.form.resource_manager_api_url = 'https://daily-cloudcode-pa.googleapis.com';
            config.form.service_usage_api_url = 'https://daily-cloudcode-pa.googleapis.com';
            config.form.antigravity_api_url = 'https://daily-cloudcode-pa.googleapis.com';
            showStatus('已充填镜像端点地址，请保存配置生效', 'info');
        };

        const restoreOfficialUrls = () => {
            config.form.code_assist_endpoint = 'https://cloudcode-pa.googleapis.com';
            config.form.oauth_proxy_url = 'https://oauth2.googleapis.com';
            config.form.googleapis_proxy_url = 'https://www.googleapis.com';
            config.form.resource_manager_api_url = 'https://cloudresourcemanager.googleapis.com';
            config.form.service_usage_api_url = 'https://serviceusage.googleapis.com';
            config.form.antigravity_api_url = 'https://daily-cloudcode-pa.googleapis.com';
            showStatus('已还原官方端点地址，请保存配置生效', 'info');
        };
        // ----------------------------------------------------------------------
        // 6. 登录与身份验证
        // ----------------------------------------------------------------------
        const login = async () => {
            if (!loginPassword.value) return showStatus('请输入访问密码', 'error');
            try {
                const res = await fetch('./auth/login', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ password: loginPassword.value })
                });
                if (res.ok) {
                    token.value = loginPassword.value;
                    localStorage.setItem('google2api_panel_password', token.value);
                    isLoggedIn.value = true;
                    showStatus('登录成功！', 'success');
                    switchTab(activeTab.value);
                    initSSE();
                } else {
                    showStatus('密码错误，登录失败', 'error');
                }
            } catch (e) {
                showStatus(`登录异常: ${e.message}`, 'error');
            }
        };

        const autoLogin = async () => {
            if (!token.value) return false;
            try {
                const res = await fetch('./auth/auto-login', {
                    method: 'POST',
                    headers: getAuthHeaders()
                });
                if (res.ok) {
                    isLoggedIn.value = true;
                    switchTab(activeTab.value);
                    return true;
                }
            } catch (e) {
                console.error('自动登录失败', e);
            }
            return false;
        };

        const logout = () => {
            localStorage.removeItem('google2api_panel_password');
            token.value = '';
            isLoggedIn.value = false;
            showStatus('已退出登录', 'info');
        };

        // ----------------------------------------------------------------------
        // 7. 定时任务（冷却更新、10 秒模型映射轮询 & 15 分钟全局额度同步）
        // ----------------------------------------------------------------------
        let cooldownInterval = null;
        let quotaInterval = null;
        let modelsInterval = null;
        let sseClient = null;

        const initSSE = () => {
            if (sseClient) {
                try { sseClient.close(); } catch (e) {}
            }

            const sseUrl = `./sse?token=${encodeURIComponent(token.value)}`;
            sseClient = new EventSource(sseUrl);

            sseClient.onopen = () => {
                console.log('[SSE] 实时推流长连接建立成功');
            };

            // 监听凭证/配额更新事件
            sseClient.addEventListener('creds_updated', (e) => {
                try {
                    const payload = JSON.parse(e.data || '{}');
                    console.log('[SSE] 收到凭证/配额变动通知:', payload);
                    const mode = payload.mode || 'antigravity';
                    if (isLoggedIn.value) {
                        fetchCreds(mode, true);
                    }
                } catch (err) {
                    console.error('[SSE] 解析 creds_updated 消息失败:', err);
                }
            });

            // 监听调度变更事件
            sseClient.addEventListener('dispatch_updated', (e) => {
                try {
                    const payload = JSON.parse(e.data || '{}');
                    console.log('[SSE] 收到账号调度更新通知:', payload);
                    const mode = payload.mode || 'antigravity';
                    if (isLoggedIn.value) {
                        if (mode === 'antigravity') ag.currentSelected = payload.selected;
                        if (mode === 'oauth') oauth.currentSelected = payload.selected;
                    }
                } catch (err) {
                    console.error('[SSE] 解析 dispatch_updated 消息失败:', err);
                }
            });

            // 监听模型映射变动事件
            sseClient.addEventListener('models_updated', () => {
                if (isLoggedIn.value && activeTab.value === 'models') {
                    loadModelMappings();
                }
            });

            // 监听 Token 看板变动事件
            sseClient.addEventListener('tokens_updated', () => {
                if (isLoggedIn.value && activeTab.value === 'tokens') {
                    loadTokenDashboard();
                }
            });

            sseClient.onerror = (err) => {
                console.warn('[SSE] 推流连接中断，浏览器将自动尝试重连...', err);
            };
        };

        onMounted(async () => {
            initTheme();
            const autoSuccess = await autoLogin();
            if (!autoSuccess) {
                showStatus('请输入密码登录控制面板', 'info');
            } else {
                initSSE();
            }

            // 1 秒本地微调计算（仅用于纯本地 CPU 计时器的倒计时数字渲染）
            cooldownInterval = setInterval(() => {
                if (isLoggedIn.value) {
                    if (activeTab.value === 'antigravity') ag.items = [...ag.items];
                    if (activeTab.value === 'oauth') oauth.items = [...oauth.items];
                }
            }, 1000);
        });

        onUnmounted(() => {
            if (cooldownInterval) clearInterval(cooldownInterval);
            if (sseClient) {
                try { sseClient.close(); } catch (e) {}
            }
        });

        return {
            copyToClipboard,
            token,
            loginPassword,
            isLoggedIn,
            activeTab,
            theme,
            isSidebarCollapsed,
            isDrawerOpen,
            toast,
            modal,
            ag,
            oauth,
            upload,
            models,
            config,
            tokenDashboard,
            loadTokenDashboard,
            clearTokenDashboard,
            formatTokenCount,
            activeTrendList,
            maxAccountTokens,
            maxModelTokens,
            login,
            logout,
            toggleTheme,
            toggleSidebarCollapse,
            toggleDrawer,
            switchTab,
            fetchCreds,
            getSelectedEmail,
            isAllSelected,
            toggleSelectAll,
            singleAction,
            switchActiveCredential,
            deleteSingleCredential,
            batchAction,
            verifyProjectId,
            batchVerifyProjectIds,
            testCredential,
            batchTestCredentials,
            toggleErrorDetails,
            batchToggleErrorDetails,
            refreshSingleQuota,
            batchRefreshEmails,
            completeOAuthFlow,
            batchRefreshQuotas,
            batchConfigurePreview,
            startAuth,
            processCallbackUrl,
            downloadAllCreds,
            getGeminiQuotaGroups,
            getQuotaBuckets,
            getBucketPercent,
            getBucketTitle,
            getBucketLabel,
            getBucketColor,
            getBucketResetTime,
            getBucketIcon,
            calculateQuotaPercent,
            getQuotaRemainingText,
            getActiveCooldowns,
            formatCooldownBadge,
            totalPages,
            getDailyQuotaAvg,
            getWeeklyQuotaAvg,
            getWeeklyQuotaUsedAvg,
            getDailyQuotaUsedAvg,
            changePage,
            cpUrl,
            cpAllUrls,
            getBaseUrl,
            handleFileSelect,
            handleFileDrop,
            doUploadFiles,
            loadModelMappings,
            saveFallbackModel,
            addModelMapping,
            deleteModelMapping,
            clearModelMappings,
            clearDynamicMappings,
            loadConfig,
            saveConfig,
            useMirrorUrls,
            restoreOfficialUrls,
            showAlert,
            showConfirm,
            handleModalConfirm,
            handleModalCancel
        };
    }
});

app.mount('#app');
