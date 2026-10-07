// Headless Teams meeting participant. Joins with the ACS Web Calling SDK and relays raw
// audio to Harbor Voice over a loopback WebSocket:
//   page -> python: binary Int16 48 kHz mono meeting audio; JSON status messages
//   python -> page: binary [uint32 id][Int16 48 kHz mono PCM] to speak; JSON commands
import { CallClient, LocalAudioStream } from "@azure/communication-calling";
import { AzureCommunicationTokenCredential } from "@azure/communication-common";

const RATE = 48000;

const CAPTURE_WORKLET = `
class Capture extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel) this.port.postMessage(channel.slice(0));
    return true;
  }
}
registerProcessor("capture", Capture);
`;

const PLAYBACK_WORKLET = `
class Playback extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = [];
    this.port.onmessage = (event) => {
      const message = event.data;
      if (message.type === "play") {
        this.queue.push({ id: message.id, samples: message.samples, offset: 0 });
      } else if (message.type === "clear") {
        for (const item of this.queue) this.port.postMessage(item.id);
        this.queue = [];
      }
    };
  }
  process(inputs, outputs) {
    const out = outputs[0][0];
    let filled = 0;
    while (filled < out.length && this.queue.length) {
      const item = this.queue[0];
      const take = Math.min(out.length - filled, item.samples.length - item.offset);
      out.set(item.samples.subarray(item.offset, item.offset + take), filled);
      item.offset += take;
      filled += take;
      if (item.offset >= item.samples.length) {
        this.queue.shift();
        this.port.postMessage(item.id);
      }
    }
    for (let i = filled; i < out.length; i++) out[i] = 0;
    return true;
  }
}
registerProcessor("playback", Playback);
`;

const params = new URLSearchParams(location.search);
const socket = new WebSocket(`ws://${location.host}/bridge?nonce=${params.get("nonce")}`);
socket.binaryType = "arraybuffer";

let call = null;
let context = null;
let playback = null;
let captureSource = null;

function send(message) {
  if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
}

function status(state, detail = "") {
  send({ type: "status", state, detail });
}

async function loadWorklet(source) {
  const url = URL.createObjectURL(new Blob([source], { type: "application/javascript" }));
  await context.audioWorklet.addModule(url);
  URL.revokeObjectURL(url);
}

async function setupAudio() {
  context = new AudioContext({ sampleRate: RATE });
  await loadWorklet(CAPTURE_WORKLET);
  await loadWorklet(PLAYBACK_WORKLET);
  const destination = context.createMediaStreamDestination();
  playback = new AudioWorkletNode(context, "playback", { outputChannelCount: [1] });
  playback.port.onmessage = (event) => send({ type: "played", id: event.data });
  playback.connect(destination);
  await context.resume();
  return destination.stream;
}

async function captureRemoteAudio() {
  if (!call || captureSource || !call.remoteAudioStreams.length) return;
  const stream = await call.remoteAudioStreams[0].getMediaStream();
  captureSource = context.createMediaStreamSource(stream);
  const capture = new AudioWorkletNode(context, "capture");
  capture.port.onmessage = (event) => {
    if (socket.readyState !== WebSocket.OPEN) return;
    const floats = event.data;
    const pcm = new Int16Array(floats.length);
    for (let i = 0; i < floats.length; i++) {
      const s = Math.max(-1, Math.min(1, floats[i]));
      pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    socket.send(pcm.buffer);
  };
  captureSource.connect(capture);
  status("listening");
}

async function join(config) {
  const outgoing = await setupAudio();
  const callClient = new CallClient();
  const credential = new AzureCommunicationTokenCredential(config.token);
  const agent = await callClient.createCallAgent(credential, { displayName: config.displayName });
  call = agent.join(
    { meetingLink: config.meetingLink },
    { audioOptions: { muted: false, localAudioStreams: [new LocalAudioStream(outgoing)] } },
  );
  call.on("stateChanged", async () => {
    const reason = call.callEndReason
      ? `${call.callEndReason.code}/${call.callEndReason.subCode}`
      : "";
    status(call.state, reason);
    if (call.state === "Connected") {
      if (call.isMuted) {
        try {
          await call.unmute();
        } catch (error) {
          status("muted", String(error));
        }
      }
      await captureRemoteAudio();
    }
  });
  call.on("remoteAudioStreamsUpdated", captureRemoteAudio);
  call.on("isMutedChanged", () => status(call.isMuted ? "muted" : "unmuted"));
}

socket.onmessage = async (event) => {
  if (event.data instanceof ArrayBuffer) {
    if (!playback) return;
    const id = new DataView(event.data).getUint32(0, true);
    const pcm = new Int16Array(event.data, 4);
    const samples = new Float32Array(pcm.length);
    for (let i = 0; i < pcm.length; i++) samples[i] = pcm[i] / 32768;
    playback.port.postMessage({ type: "play", id, samples }, [samples.buffer]);
    return;
  }
  const message = JSON.parse(event.data);
  try {
    if (message.type === "join") await join(message);
    else if (message.type === "clear" && playback) playback.port.postMessage({ type: "clear" });
    else if (message.type === "leave" && call) await call.hangUp();
  } catch (error) {
    status("error", String(error && error.message ? error.message : error));
  }
};

socket.onopen = () => status("ready");
