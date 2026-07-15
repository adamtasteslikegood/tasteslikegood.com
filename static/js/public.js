(() => {
    const modal = document.getElementById("spa-modal");
    const modalCard = modal?.querySelector('[role="dialog"]');
    const toast = document.getElementById("public-toast");
    let lastFocused = null;
    const openKitchen = () => {
        window.location.href = "/#kitchen";
    };
    const focusableSelector =
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
    const focusableElements = () =>
        modal ? Array.from(modal.querySelectorAll(focusableSelector)) : [];
    const closeModal = () => {
        if (!modal) return;
        modal.classList.remove("is-open");
        modal.setAttribute("aria-hidden", "true");
        if (lastFocused instanceof HTMLElement) {
            lastFocused.focus();
        }
        lastFocused = null;
    };

    document.querySelectorAll("[data-open-kitchen]").forEach((button) => {
        button.addEventListener("click", () => {
            if (!modal) {
                openKitchen();
                return;
            }
            lastFocused = button;
            modal.classList.add("is-open");
            modal.setAttribute("aria-hidden", "false");
            const [firstFocusable] = focusableElements();
            (firstFocusable || modalCard)?.focus();
        });
    });

    document.querySelectorAll("[data-close-modal]").forEach((button) => {
        button.addEventListener("click", closeModal);
    });

    if (modal) {
        modal.addEventListener("click", (event) => {
            if (event.target === modal) {
                closeModal();
            }
        });
        modal.addEventListener("keydown", (event) => {
            if (event.key === "Escape") {
                event.preventDefault();
                closeModal();
                return;
            }
            if (event.key !== "Tab") return;

            const elements = focusableElements();
            if (!elements.length) {
                event.preventDefault();
                modalCard?.focus();
                return;
            }

            const first = elements[0];
            const last = elements[elements.length - 1];
            if (event.shiftKey && document.activeElement === first) {
                event.preventDefault();
                last.focus();
            } else if (!event.shiftKey && document.activeElement === last) {
                event.preventDefault();
                first.focus();
            }
        });
    }

    const saveButton = document.querySelector("[data-save-recipe]");
    if (saveButton && toast) {
        saveButton.addEventListener("click", () => {
            saveButton.classList.add("is-saved");
            toast.textContent = "Opening your kitchen...";
            toast.hidden = false;
        });
    }
})();
