(() => {
    const toast = document.getElementById("public-toast");

    const saveButton = document.querySelector("[data-save-recipe]");
    if (saveButton && toast) {
        saveButton.addEventListener("click", () => {
            saveButton.classList.add("is-saved");
            toast.textContent = "Opening your kitchen...";
            toast.hidden = false;
        });
    }
})();
