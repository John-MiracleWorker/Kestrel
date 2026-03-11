const WebSocket = require('ws');
const ws = new WebSocket(
    'ws://localhost:8741/ws?workspaceId=e3a733ea-71c5-435f-bc6e-cc9e6c4eec10&token=dev-secret-change-me',
);
ws.on('open', () => {
    ws.send(
        JSON.stringify({ type: 'CHAT_MESSAGE', payload: { content: 'hello from test script!' } }),
    );
});
ws.on('message', (data) => console.log('RCV:', data.toString()));
