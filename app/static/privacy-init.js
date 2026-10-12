// Runs in <head> before any rendering: apply privacy mode so real figures never flash on load.
try { if (localStorage.getItem("satoshi-privacy") === "1") document.documentElement.classList.add("priv"); } catch (e) {}
