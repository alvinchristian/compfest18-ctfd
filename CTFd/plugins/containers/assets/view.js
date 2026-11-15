CTFd._internal.challenge.data = undefined;
CTFd._internal.challenge.renderer = null;
CTFd._internal.challenge.preRender = function() { };
CTFd._internal.challenge.render = null;
CTFd._internal.challenge.postRender = function() { };

CTFd._internal.challenge.submit = function(preview) {
    var challenge_id = parseInt(CTFd.lib.$("#challenge-id").val());
    var submission = CTFd.lib.$("#challenge-input").val();

    let alert = resetAlert();

    var body = {
        challenge_id: challenge_id,
        submission: submission,
    };
    var params = {};
    if (preview) {
        params["preview"] = true;
    }

    return CTFd.api
        .post_challenge_attempt(params, body)
        .then(function(response) {
            if (response.status === 429) return response; // Rate limit
            if (response.status === 403) return response; // Not logged in / CTF paused
            return response;
        });
};

var _expiryInterval = null;
var _queuedRequestUuid = null;

function clearExpiryInterval() {
    if (_expiryInterval) {
        clearInterval(_expiryInterval);
        _expiryInterval = null;
    }
}

function mergeQueryParams(parameters, queryParameters) {
    if (parameters.$queryParameters) {
        Object.keys(parameters.$queryParameters).forEach(function(parameterName) {
            queryParameters[parameterName] = parameters.$queryParameters[parameterName];
        });
    }
    return queryParameters;
}

function resetAlert() {
    clearExpiryInterval();
    let status = document.getElementById("deployment-info");
    status.innerHTML = '<div class="d-flex align-items-center gap-2 text-muted"><div class="spinner-border spinner-border-sm" role="status"></div><span>Loading...</span></div>';
    status.classList.remove("container-status-error");

    // Disable buttons while loading
    document.getElementById("create-chal").disabled = true;
    document.getElementById("extend-chal").disabled = true;
    document.getElementById("terminate-chal").disabled = true;
    const queueBtn = document.getElementById("exit-queue");
    if (queueBtn) queueBtn.disabled = true;

    return status;
}

function enableButtons() {
    document.getElementById("create-chal").disabled = false;
    document.getElementById("extend-chal").disabled = false;
    document.getElementById("terminate-chal").disabled = false;
    const queueBtn = document.getElementById("exit-queue");
    if (queueBtn) queueBtn.disabled = false;
}

function toggleChallengeCreate() {
    const btn = document.getElementById("create-chal");
    if (btn) {
        btn.classList.remove('d-none');
    }
}

function hideChallengeCreate() {
    const btn = document.getElementById("create-chal");
    if (btn) {
        btn.classList.add('d-none');
    }
}

function toggleChallengeUpdate() {
    const extendBtn = document.getElementById("extend-chal");
    const terminateBtn = document.getElementById("terminate-chal");
    if (extendBtn) extendBtn.classList.remove('d-none');
    if (terminateBtn) terminateBtn.classList.remove('d-none');
    hideQueueExit();
}

function hideChallengeUpdate() {
    const extendBtn = document.getElementById("extend-chal");
    const terminateBtn = document.getElementById("terminate-chal");
    if (extendBtn) extendBtn.classList.add('d-none');
    if (terminateBtn) terminateBtn.classList.add('d-none');
}

function showQueueExit(queueUuid) {
    _queuedRequestUuid = queueUuid;
    const queueBtn = document.getElementById("exit-queue");
    if (queueBtn) queueBtn.classList.remove('d-none');
}

function hideQueueExit() {
    const queueBtn = document.getElementById("exit-queue");
    if (queueBtn) queueBtn.classList.add('d-none');
}

function calculateExpiry(date) {
    return Math.ceil((new Date(date * 1000) - new Date()) / 1000 / 60);
}

function formatExpiry(timestampMs) {
    const now = Date.now()
    const secondsLeft = Math.max(0, Math.ceil((timestampMs - now) / 1000));
    if (secondsLeft <= 0) {
        return "Expired";
    }
    const m = Math.floor(secondsLeft / 60);
    const s = secondsLeft % 60;
    return String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
}

function renderConnectionInfo(parent, data) {
    parent.innerHTML = "";
    parent.style.display = "flex";
    parent.style.alignItems = "center";
    parent.style.justifyContent = "space-between";
    parent.style.gap = "0.5rem";

    // Left side: connection string + extra info
    let left = document.createElement('div');
    left.style.overflow = "hidden";
    left.style.textOverflow = "ellipsis";

    // Connection string
    let connStr;
    if (data.connection.type == "tcp" || data.connection.type == "nc") {
        connStr = 'nc ' + data.connection.host + " " + data.connection.port;
    } else if (data.connection.type == "ssh") {
        connStr = 'ssh -p ' + data.connection.port + ' user@' + data.connection.host;
    } else if (data.connection.type == "http") {
        connStr = 'http://' + data.connection.host + ":" + data.connection.port;
    } else {
        connStr = data.connection.host + ":" + data.connection.port;
    }

    if (data.connection.type == "http") {
        let link = document.createElement('a');
        link.href = connStr;
        link.textContent = connStr;
        link.target = '_blank';
        link.style.fontFamily = 'var(--bs-font-monospace)';
        left.append(link);
    } else {
        let code = document.createElement('code');
        code.textContent = connStr;
        left.append(code);
    }

    if (data.connection.auth_proxy !== false) {
        let authBr = document.createElement('br');
        let authInfo = document.createElement('small');
        authInfo.className = 'text-muted';
        if (data.connection.type == "http") {
            authInfo.textContent = "Enter your CTFd access token on the access page.";
        } else {
            authInfo.textContent = "Enter your CTFd access token when prompted.";
        }
        left.append(authBr, authInfo);
    }

    // Right side: expiry countdown
    let expiresAt = data.expires_at;
    let expiry = document.createElement('span');
    expiry.className = 'container-expiry';
    expiry.style.whiteSpace = 'nowrap';
    expiry.style.flexShrink = '0';
    expiry.innerHTML = '<i class="fas fa-clock me-1"></i>' + formatExpiry(expiresAt);

    parent.append(left, expiry);

    // Live countdown
    let challengeId = data.challenge_id || (parent.closest('[data-challenge-id]') && parseInt(parent.closest('[data-challenge-id]').getAttribute('data-challenge-id')));
    clearExpiryInterval();
    _expiryInterval = setInterval(function() {
        let text = formatExpiry(expiresAt);
        expiry.innerHTML = '<i class="fas fa-clock me-1"></i>' + text;
        if (text === "Expired") {
            clearExpiryInterval();
            if (challengeId) {
                view_container_info(challengeId);
            }
        }
    }, 1000);
}

function view_container_info(challenge_id) {
    let status = resetAlert();

    fetch("/api/v1/containers/info/" + challenge_id, {
        method: "GET",
        headers: {
            "Accept": "application/json",
            "CSRF-Token": init.csrfNonce
        }
    })
        .then(response => response.json())
        .then(data => {
            status.innerHTML = "";

            if (data.status == "not_found") {
                let msg = document.createElement('span');
                msg.className = 'text-muted';
                msg.textContent = "No active instance.";
                status.append(msg);
                hideChallengeUpdate();
                toggleChallengeCreate();
            } else if (data.status == "running" || data.status == "provisioning") {
                renderConnectionInfo(status, data);
                hideChallengeCreate();
                toggleChallengeUpdate();
            } else {
                status.textContent = data.error || "Unknown status";
                status.classList.add("container-status-error");
                hideChallengeUpdate();
                toggleChallengeCreate();
            }
        })
        .catch(error => {
            console.error("[Container] Fetch error:", error);
            status.textContent = "Error fetching container info.";
            status.classList.add("container-status-error");
            toggleChallengeCreate();
        })
        .finally(enableButtons);
}

function container_request(challenge_id) {
    let status = resetAlert();

    fetch("/api/v1/containers/request", {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "CSRF-Token": init.csrfNonce
        },
        body: JSON.stringify({ challenge_id: challenge_id })
    })
        .then(response => response.json())
        .then(data => {
            status.innerHTML = "";
            if (data.status === "queued") {
                status.textContent = "All container ports are currently in use. Your request has been queued; it will be started when a port becomes available.";
                status.classList.add("container-status-error");
                hideChallengeCreate();
                hideChallengeUpdate();
                showQueueExit(data.queue_uuid);
            } else if (data.error) {
                status.textContent = data.error;
                status.classList.add("container-status-error");
                toggleChallengeCreate();
            } else {
                renderConnectionInfo(status, data);
                hideChallengeCreate();
                toggleChallengeUpdate();
            }
        })
        .catch(error => {
            console.error("[Container] Request error:", error);
            status.textContent = "Error requesting container.";
            status.classList.add("container-status-error");
        })
        .finally(enableButtons);
}

function container_cancel_queue() {
    if (!_queuedRequestUuid) return;

    let status = resetAlert();
    fetch("/api/v1/containers/queue/cancel", {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "CSRF-Token": init.csrfNonce
        },
        body: JSON.stringify({ queue_uuid: _queuedRequestUuid })
    })
        .then(response => response.json())
        .then(data => {
            if (data.error) {
                status.textContent = data.error;
                status.classList.add("container-status-error");
                return;
            }
            _queuedRequestUuid = null;
            status.textContent = "You have exited the container queue.";
            hideQueueExit();
            toggleChallengeCreate();
        })
        .catch(error => {
            console.error("[Container] Queue cancellation error:", error);
            status.textContent = "Error exiting the queue.";
            status.classList.add("container-status-error");
        })
        .finally(enableButtons);
}

function container_renew(challenge_id) {
    let status = resetAlert();

    fetch("/api/v1/containers/renew", {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "CSRF-Token": init.csrfNonce
        },
        body: JSON.stringify({ challenge_id: challenge_id })
    })
        .then(response => response.json())
        .then(data => {
            status.innerHTML = "";
            if (data.error) {
                status.textContent = data.error;
                status.classList.add("container-status-error");
                setTimeout(function() {
                    fetch("/api/v1/containers/info/" + challenge_id, {
                        method: "GET",
                        headers: {
                            "Accept": "application/json",
                            "CSRF-Token": init.csrfNonce
                        }
                    })
                        .then(response => response.json())
                        .then(instanceData => {
                            if (
                                instanceData.status === "running" ||
                                instanceData.status === "provisioning"
                            ) {
                                renderConnectionInfo(status, instanceData);
                                hideChallengeCreate();
                                toggleChallengeUpdate();
                            } else {
                                view_container_info(challenge_id);
                            }
                        })
                        .catch(() => view_container_info(challenge_id));
                }, 2000);
            } else {
                view_container_info(challenge_id);
            }
        })
        .catch(error => {
            status.textContent = "Error renewing container.";
            status.classList.add("container-status-error");
            console.error("Fetch error:", error);
        })
        .finally(enableButtons);
}

function container_stop(challenge_id) {
    let status = resetAlert();

    fetch("/api/v1/containers/stop", {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "CSRF-Token": init.csrfNonce
        },
        body: JSON.stringify({ challenge_id: challenge_id })
    })
        .then(response => response.json())
        .then(data => {
            status.innerHTML = "";
            if (data.error) {
                status.textContent = data.error;
                status.classList.add("container-status-error");
            } else {
                let msg = document.createElement('span');
                msg.className = 'text-muted';
                msg.textContent = "Instance terminated.";
                status.append(msg);
                hideChallengeUpdate();
                toggleChallengeCreate();
            }
        })
        .catch(error => {
            console.error("[Container] Stop error:", error);
            status.textContent = "Error stopping container.";
            status.classList.add("container-status-error");
        })
        .finally(enableButtons);
}

// Initialize: Inject UI elements if template doesn't load
(function() {
    // console.log("[Container] Initializing - checking for template render");

    let checkCount = 0;
    const maxChecks = 20;

    function checkAndInject() {
        checkCount++;
        // console.log(`[Container] Check #${checkCount}: Looking for deployment-actions or challenge window`);

        // First check if our template loaded
        let deploymentDiv = document.querySelector('.deployment-actions');
        if (deploymentDiv) {
            const challengeId = deploymentDiv.getAttribute('data-challenge-id');
            if (challengeId) {
                // console.log("[Container] Template loaded! Challenge ID:", challengeId);
                view_container_info(parseInt(challengeId));
                return true;
            }
        }

        // If template didn't load, inject UI manually
        const challengeWindow = document.getElementById('challenge-window');
        const challengeBody = challengeWindow ? challengeWindow.querySelector('.modal-body') : null;

        if (challengeBody && !document.querySelector('.deployment-actions-injected')) {
            // console.log("[Container] Template NOT loaded, injecting UI manually");

            // Try multiple ways to get challenge ID
            let challengeId = null;

            // Method 1: From window.challenge
            if (window.challenge?.data?.id) {
                challengeId = window.challenge.data.id;
                // console.log("[Container] Got challenge ID from window.challenge:", challengeId);
            }

            // Method 2: From CTFd internal store
            if (!challengeId && window.CTFd?._internal?.challenge?.data?.id) {
                challengeId = window.CTFd._internal.challenge.data.id;
                // console.log("[Container] Got challenge ID from CTFd._internal:", challengeId);
            }

            // Method 3: From challenge-id input field
            if (!challengeId) {
                const challengeIdInput = document.getElementById('challenge-id');
                if (challengeIdInput) {
                    challengeId = parseInt(challengeIdInput.value);
                    // console.log("[Container] Got challenge ID from input field:", challengeId);
                }
            }

            // Method 4: From modal title or data attributes
            if (!challengeId && challengeWindow) {
                const titleElement = challengeWindow.querySelector('[data-challenge-id]');
                if (titleElement) {
                    challengeId = parseInt(titleElement.getAttribute('data-challenge-id'));
                    // console.log("[Container] Got challenge ID from data attribute:", challengeId);
                }
            }

            if (!challengeId) {
                console.warn("[Container] Cannot get challenge ID, available data:", {
                    'window.challenge': window.challenge,
                    'CTFd._internal.challenge': window.CTFd?._internal?.challenge,
                    'challenge-id input': document.getElementById('challenge-id')?.value
                });
                return false;
            }

            // console.log("[Container] Using challenge ID:", challengeId);

            // Create container UI
            const containerDiv = document.createElement('div');
            containerDiv.className = 'deployment-actions-injected deployment-actions';
            containerDiv.innerHTML = `
                <div class="challenge-connection-box">
                    <div class="container-status" id="deployment-info">
                        <div class="d-flex align-items-center gap-2 text-muted">
                            <div class="spinner-border spinner-border-sm" role="status"></div>
                            <span>Loading instance status...</span>
                        </div>
                    </div>
                </div>
                <div class="container-actions d-flex align-items-stretch gap-2" style="white-space:nowrap">
                    <button onclick="container_request(${challengeId})" class="btn btn-primary btn-sm auth-submit-button instance-launch-button d-none" id="create-chal">
                        <span><i class="fas fa-play me-1"></i> Launch Instance</span>
                    </button>
                    <button onclick="container_renew(${challengeId})" class="btn btn-sm instance-extend-button d-none" id="extend-chal">
                        <span><i class="fas fa-clock me-1"></i> Extend +5m</span>
                    </button>
                    <button onclick="container_stop(${challengeId})" class="btn btn-outline-danger btn-sm d-none px-0" style="aspect-ratio:1" id="terminate-chal">
                        <i class="fas fa-stop"></i>
                    </button>
                    <button onclick="container_cancel_queue()" class="btn btn-danger btn-sm d-none" id="exit-queue">
                        <i class="fas fa-sign-out-alt me-1"></i> Exit from queue
                    </button>
                </div>
            `;

            // Insert after challenge description
            const descSection = challengeBody.querySelector('.challenge-desc');
            if (descSection) {
                descSection.after(containerDiv);
                // console.log("[Container] UI injected, calling view_container_info");
                view_container_info(challengeId);
                return true;
            } else {
                challengeBody.insertBefore(containerDiv, challengeBody.firstChild);
                // console.log("[Container] UI injected at top, calling view_container_info");
                view_container_info(challengeId);
                return true;
            }
        }

        if (checkCount >= maxChecks) {
            console.error("[Container] Max checks reached, giving up");
            return true; // Stop checking
        }

        return false;
    }

    // Don't try immediately - the DOM may still show a previous challenge's
    // content since Alpine hasn't updated it yet. Always use the
    // MutationObserver to wait for the correct DOM state.

    // Watch for changes
    const observer = new MutationObserver(function(mutations) {
        if (checkAndInject()) {
            observer.disconnect();
        }
    });

    observer.observe(document.body, {
        childList: true,
        subtree: true
    });

    // console.log("[Container] MutationObserver started");

    // Clean up countdown on modal close
    var challengeModal = document.getElementById('challenge-window');
    if (challengeModal) {
        challengeModal.addEventListener('hidden.bs.modal', function() {
            clearExpiryInterval();
        });
    }
})();
