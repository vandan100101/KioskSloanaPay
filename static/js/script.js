document.addEventListener("DOMContentLoaded", async () => {
    // Only run on index page
    if (!document.getElementById("qr-section")) return;
    
    const qrSection = document.getElementById("qr-section");
    const loader = document.getElementById("loader");
    const loadingText = document.getElementById("loading-text");
    const statusMsg = document.getElementById("status-message");

    try {
        // Create payment and get QR code
        const response = await fetch("/create_payment", { method: "POST" });
        const data = await response.json();

        if (!data.qr_image) {
            loadingText.innerText = "⚠️ Failed to generate QR code.";
            loadingText.style.color = "#ff5252";
            loader.style.display = "none";
            return;
        }

        // Display QR code
        loader.style.display = "none";
        loadingText.innerText = "";
        
        const img = document.createElement("img");
        img.src = `data:image/png;base64,${data.qr_image}`;
        img.alt = "Payment QR Code";
        qrSection.appendChild(img);

        const amountText = document.createElement("p");
        amountText.textContent = `Amount: ${data.amount}`;
        amountText.style.marginTop = "15px";
        amountText.style.fontSize = "1.2em";
        amountText.style.fontWeight = "600";
        qrSection.appendChild(amountText);

        const reference = data.reference;

        // Poll for payment status
        const poll = setInterval(async () => {
            try {
                const res = await fetch(`/check_payment/${reference}`);
                const statusData = await res.json();
                const status = statusData.status;

                if (status === "PAID") {
                    clearInterval(poll);
                    statusMsg.className = "success";
                    statusMsg.innerHTML = "✅ Payment Received!<br>🧼 Sanitizing helmet...";
                    
                    setTimeout(() => {
                        statusMsg.innerHTML = "✨ Sanitization Complete!<br>Have a safe ride!";
                    }, 3000);
                    
                    setTimeout(() => {
                        window.location.reload();
                    }, 7000);
                    
                } else if (status === "FAILED" || status === "CANCELLED") {
                    clearInterval(poll);
                    statusMsg.className = "fail";
                    statusMsg.innerHTML = "❌ Payment Failed. Reloading...";
                    setTimeout(() => window.location.reload(), 4000);
                    
                } else {
                    statusMsg.innerHTML = "⏳ Waiting for payment...";
                }
            } catch (err) {
                console.error("Polling error:", err);
            }
        }, 5000); // Check every 5 seconds

        // Stop polling after 10 minutes
        setTimeout(() => {
            clearInterval(poll);
            if (statusMsg.className !== "success") {
                statusMsg.className = "fail";
                statusMsg.innerHTML = "⏱️ Payment timeout. Reloading...";
                setTimeout(() => window.location.reload(), 3000);
            }
        }, 600000);

    } catch (err) {
        console.error("Error:", err);
        loadingText.innerText = "❌ Error contacting server. Please check your connection.";
        loadingText.style.color = "#ff5252";
        loader.style.display = "none";
    }
});