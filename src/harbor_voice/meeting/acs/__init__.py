"""Azure-backed meeting participant: Communication Services join plus neural speech.

Scout joins a Teams meeting as its own guest participant ("Scout (AI)") through a headless
Microsoft Edge page running the Azure Communication Services Web Calling SDK. Raw meeting
audio flows over a loopback-only WebSocket into the same meeting agent used by local meeting
mode, and spoken answers flow back the same way. Authentication uses Microsoft Entra ID via
the Azure CLI sign-in, so no keys are stored.
"""
