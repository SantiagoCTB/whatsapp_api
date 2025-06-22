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
}
