import Alpine from "alpinejs";
import dayjs from "dayjs";

import CTFd from "./index";

import { Modal, Tooltip } from "bootstrap";
import highlight from "./theme/highlight";

const NO_AI_DECLARATION = "I did not use AI to solve this challenge.";

function addTargetBlank(html) {
  let dom = new DOMParser();
  let view = dom.parseFromString(html, "text/html");
  let links = view.querySelectorAll('a[href*="://"]');
  links.forEach(link => {
    link.setAttribute("target", "_blank");
  });
  return view.documentElement.outerHTML;
}

window.Alpine = Alpine;

Alpine.store("challenge", {
  data: {
    view: "",
  },
});

Alpine.data("Hint", () => ({
  id: null,
  html: null,
  hintVisible: false,

  async unlockHint() {
    let response = await CTFd.pages.challenge.loadHint(this.id);

    if (response.errors) {
      CTFd._functions.challenge.displayUnlockError(response);
      return;
    }
    let hint = response.data;
    if (hint.content) {
      this.html = addTargetBlank(hint.html);
      this.hintVisible = true;
    } else {
      let answer = await CTFd.pages.challenge.displayUnlock(this.id);
      if (answer) {
        let unlock = await CTFd.pages.challenge.loadUnlock(this.id);

        if (unlock.success) {
          let response = await CTFd.pages.challenge.loadHint(this.id);
          let hint = response.data;
          this.html = addTargetBlank(hint.html);
          this.hintVisible = true;
        } else {
          CTFd._functions.challenge.displayUnlockError(unlock);
        }
      }
    }
  },
}));

Alpine.data("Challenge", () => ({
  id: null,
  next_id: null,
  submission: "",
  aiLinks: [""],
  submitting: false,
  tab: null,
  solves: [],
  submissions: [],
  submissionsVisible: false,
  solution: null,
  solutionVisible: false,
  response: null,
  share_url: null,
  max_attempts: 0,
  attempts: 0,
  ratingValue: 0,
  selectedRating: 0,
  ratingReview: "",
  ratingSubmitted: false,
  solvesCollapsed: false,

  async init() {
    highlight();
    try {
      this.solvesCollapsed =
        localStorage.getItem("challenge-solves-collapsed") === "true";
    } catch (error) {
      this.solvesCollapsed = false;
    }
  },

  async loadSolves() {
    this.solves = await CTFd.pages.challenge.loadSolves(this.id);
    this.solves.forEach(solve => {
      solve.date = dayjs(solve.date).format("MMMM Do, h:mm:ss A");
      return solve;
    });
  },

  toggleSolves() {
    this.solvesCollapsed = !this.solvesCollapsed;
    try {
      localStorage.setItem("challenge-solves-collapsed", this.solvesCollapsed);
    } catch (error) {
      // Ignore storage errors (private mode, blocked storage, etc.)
    }
  },

  async showSubmissions() {
    let response = await CTFd.pages.users.userSubmissions("me", this.id);
    this.submissions = response.data;
    this.submissions.forEach(s => {
      s.date = dayjs(s.date).format("MMMM Do, h:mm:ss A");
      return s;
    });
    this.submissionsVisible = true;
  },

  getSolutionId() {
    let data = Alpine.store("challenge").data;
    return data.solution_id;
  },

  getSolutionState() {
    let data = Alpine.store("challenge").data;
    return data.solution_state;
  },

  setSolutionId(solutionId) {
    Alpine.store("challenge").data.solution_id = solutionId;
  },

  async showSolution() {
    let solution_id = this.getSolutionId();
    CTFd._functions.challenge.displaySolution = solution => {
      this.solution = solution.html;
    };
    await CTFd.pages.challenge.displaySolution(solution_id);
    this.solutionVisible = true;
  },

  getNextId() {
    let data = Alpine.store("challenge").data;
    return data.next_id;
  },

  async nextChallenge() {
    let modal = Modal.getOrCreateInstance("[x-ref='challengeWindow']");

    // TODO: Get rid of this private attribute access
    // See https://github.com/twbs/bootstrap/issues/31266
    modal._element.addEventListener(
      "hidden.bs.modal",
      event => {
        // Dispatch load-challenge event to call loadChallenge in the ChallengeBoard
        Alpine.nextTick(() => {
          this.$dispatch("load-challenge", this.getNextId());
        });
      },
      { once: true },
    );
    modal.hide();
  },

  async getShareUrl() {
    let body = {
      type: "solve",
      challenge_id: this.id,
    };
    const response = await CTFd.fetch("/api/v1/shares", {
      method: "POST",
      body: JSON.stringify(body),
    });
    const data = await response.json();
    const url = data["data"]["url"];
    this.share_url = url;
  },

  copyShareUrl() {
    navigator.clipboard.writeText(this.share_url);
    let t = Tooltip.getOrCreateInstance(this.$el);
    t.enable();
    t.show();
    setTimeout(() => {
      t.hide();
      t.disable();
    }, 2000);
  },

  addAiLink() {
    if (this.aiLinks.length < 20) {
      this.aiLinks.push("");
    }
  },

  removeAiLink(index) {
    if (this.aiLinks.length > 1) {
      this.aiLinks.splice(index, 1);
    }
  },

  setArtifactError(message) {
    this.response = {
      success: true,
      data: { status: "invalid", message },
    };
  },

  async submitChallenge() {
    const aiLinks = this.aiLinks.map(link => link.trim()).filter(Boolean);
    const solverFile = this.$refs.solverFile?.files?.[0];
    if (!aiLinks.length) {
      this.setArtifactError(
        `Add at least one AI link or enter "${NO_AI_DECLARATION}".`,
      );
      return;
    }
    if (aiLinks.includes(NO_AI_DECLARATION) && aiLinks.length !== 1) {
      this.setArtifactError("The no-AI declaration cannot be combined with AI links.");
      return;
    }
    if (
      !aiLinks.every(
        link => link === NO_AI_DECLARATION || /^https?:\/\/[^\s]+$/i.test(link),
      )
    ) {
      this.setArtifactError(
        "Each entry must be a valid HTTP or HTTPS URL, or the exact no-AI declaration.",
      );
      return;
    }
    if (!solverFile) {
      this.setArtifactError("Upload the solver file before submitting.");
      return;
    }

    const formData = new FormData();
    formData.append("nonce", CTFd.config.csrfNonce);
    formData.append("challenge_id", this.id);
    formData.append("submission", this.submission);
    aiLinks.forEach(link => formData.append("ai_links", link));
    formData.append("solver_file", solverFile);

    this.submitting = true;
    try {
      const root = (CTFd.config.urlRoot || "").replace(/\/$/, "");
      const response = await window.fetch(`${root}/api/v1/challenges/attempt`, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
          "CSRF-Token": CTFd.config.csrfNonce,
        },
        body: formData,
      });
      this.response = await response.json();
      if (CTFd._functions.challenge.displaySubmissionResponse) {
        CTFd._functions.challenge.displaySubmissionResponse(this.response);
      }
    } catch (_error) {
      this.setArtifactError("The solve could not be submitted. Please try again.");
    } finally {
      this.submitting = false;
    }

    await this.renderSubmissionResponse();
  },

  async renderSubmissionResponse() {
    if (this.response.data.status === "correct") {
      this.submission = "";
      this.aiLinks = [""];
      this.$refs.solverFile.value = "";
    }

    // Decide whether to check for the solution
    if (this.getSolutionId() == null) {
      if (
        CTFd.pages.challenge.checkSolution(
          this.getSolutionState(),
          Alpine.store("challenge").data,
          this.response.data.status,
        )
      ) {
        let data = await CTFd.pages.challenge.getSolution(this.id);
        this.setSolutionId(data.id);
      }
    }

    // Increment attempts counter
    if (
      this.max_attempts > 0 &&
      this.response.data.status != "already_solved" &&
      this.response.data.status != "ratelimited"
    ) {
      this.attempts += 1;
    }

    // Dispatch load-challenges event to call loadChallenges in the ChallengeBoard
    this.$dispatch("load-challenges");
  },

  async submitRating() {
    const response = await CTFd.pages.challenge.submitRating(
      this.id,
      this.selectedRating,
      this.ratingReview,
    );
    if (response.value) {
      this.ratingValue = this.selectedRating;
      this.ratingSubmitted = true;
    } else {
      alert("Error submitting rating");
    }
  },
}));

Alpine.data("ChallengeBoard", () => ({
  loaded: false,
  challenges: [],
  challenge: null,
  searchQuery: "",
  selectedCategories: [],
  hideSolved: false,

  async init() {
    this.challenges = await CTFd.pages.challenges.getChallenges();
    this.loaded = true;

    if (window.location.hash) {
      let chalHash = decodeURIComponent(window.location.hash.substring(1));
      let idx = chalHash.lastIndexOf("-");
      if (idx >= 0) {
        let pieces = [chalHash.slice(0, idx), chalHash.slice(idx + 1)];
        let id = pieces[1];
        await this.loadChallenge(id);
      }
    }
  },

  getAllCategories() {
    const categories = [];

    this.challenges.forEach(challenge => {
      const { category } = challenge;
      if (!categories.includes(category)) {
        categories.push(category);
      }
    });

    try {
      const f = CTFd.config.themeSettings.challenge_category_order;
      if (f) {
        const getSort = new Function(`return (${f})`);
        categories.sort(getSort());
      }
    } catch (error) {
      console.log("Error running challenge_category_order function");
      console.log(error);
    }

    return categories;
  },

  getCategories() {
    let categories = this.getAllCategories();

    if (this.selectedCategories.length > 0) {
      categories = categories.filter(c => this.selectedCategories.includes(c));
    }

    // Hide categories with no matching challenges
    categories = categories.filter(c => this.getChallenges(c).length > 0);

    return categories;
  },

  toggleCategory(cat) {
    const idx = this.selectedCategories.indexOf(cat);
    if (idx === -1) {
      this.selectedCategories.push(cat);
    } else {
      this.selectedCategories.splice(idx, 1);
    }
  },

  getChallenges(category) {
    let challenges = this.challenges;

    if (category !== null) {
      challenges = challenges.filter(challenge => challenge.category === category);
    }

    if (this.searchQuery) {
      const query = this.searchQuery.toLowerCase();
      challenges = challenges.filter(challenge =>
        challenge.name.toLowerCase().includes(query),
      );
    }

    if (this.hideSolved) {
      challenges = challenges.filter(challenge => !challenge.solved_by_me);
    }

    try {
      const f = CTFd.config.themeSettings.challenge_order;
      if (f) {
        const getSort = new Function(`return (${f})`);
        challenges.sort(getSort());
      }
    } catch (error) {
      // Ignore errors with theme challenge sorting
      console.log("Error running challenge_order function");
      console.log(error);
    }

    return challenges;
  },

  categoryColor(category) {
    let hash = 0;
    for (let i = 0; i < category.length; i++) {
      hash = category.charCodeAt(i) + ((hash << 5) - hash);
    }
    return "#" + ((hash >>> 0) & 0xffffff).toString(16).padStart(6, "0");
  },

  async loadChallenges() {
    this.challenges = await CTFd.pages.challenges.getChallenges();
  },

  async loadChallenge(challengeId) {
    await CTFd.pages.challenge.displayChallenge(challengeId, challenge => {
      challenge.data.view = addTargetBlank(challenge.data.view);
      Alpine.store("challenge").data = challenge.data;

      // nextTick is required here because we're working in a callback
      Alpine.nextTick(() => {
        let modal = Modal.getOrCreateInstance("[x-ref='challengeWindow']");
        // TODO: Get rid of this private attribute access
        // See https://github.com/twbs/bootstrap/issues/31266
        modal._element.addEventListener(
          "hidden.bs.modal",
          event => {
            // Remove location hash
            history.replaceState(null, null, " ");
          },
          { once: true },
        );
        modal.show();
        history.replaceState(null, null, `#${challenge.data.name}-${challengeId}`);
      });
    });
  },
}));

Alpine.start();
