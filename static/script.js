function selectChat(ticket_id, numero, mensaje, timestamp) {
    const chatContent = document.getElementById('chat-content');
    chatContent.innerHTML = `
        <p><strong>Cliente:</strong> ${numero}</p>
        <p>${mensaje}</p>
        <p><em>${timestamp}</em></p>
    `;

    document.getElementById('ticket_id').value = ticket_id;
    document.getElementById('numero').value = numero;

    // Cambiar la acción del formulario dinámicamente
    document.getElementById('replyForm').action = '/responder/' + ticket_id;

    document.getElementById('imageForm').addEventListener('submit', async e => {
        e.preventDefault();
        if (!currentChat) return alert("Selecciona un chat");

        const form = new FormData();
        form.append('numero', currentChat);
        form.append('image', document.getElementById('imageInput').files[0]);
        form.append('caption', document.getElementById('captionInput').value);

        await fetch('/send_image', { method: 'POST', body: form });
        document.getElementById('imageInput').value = '';
        document.getElementById('captionInput').value = '';
        fetchChat();
        fetchChatList();
    });
    
}
