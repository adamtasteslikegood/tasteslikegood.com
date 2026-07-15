(() => {
    const modal = document.getElementById("spa-modal");
    const toast = document.getElementById("public-toast");
    const openKitchen = () => {
        window.location.href = "/#kitchen";
    };

    document.querySelectorAll("[data-open-kitchen]").forEach((button) => {
        button.addEventListener("click", () => {
            if (!modal) {
                openKitchen();
                return;
            }
            modal.classList.add("is-open");
            modal.setAttribute("aria-hidden", "false");
        });
    });

    document.querySelectorAll("[data-close-modal]").forEach((button) => {
        button.addEventListener("click", () => {
            if (!modal) return;
            modal.classList.remove("is-open");
            modal.setAttribute("aria-hidden", "true");
        });
    });

    if (modal) {
        modal.addEventListener("click", (event) => {
            if (event.target === modal) {
                modal.classList.remove("is-open");
                modal.setAttribute("aria-hidden", "true");
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
