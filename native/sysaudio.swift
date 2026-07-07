// sysaudio — przechwytywanie dźwięku systemowego (loopback) przez ScreenCaptureKit.
//
// Nagrywa cały dźwięk systemu (to, co słychać z Zoom/Chrome/itd.) do pliku WAV.
// Uruchomienie:  sysaudio <plik-wyjściowy.wav>
// Zatrzymanie:   SIGINT lub SIGTERM — plik jest wtedy poprawnie domykany.
//
// Komunikaty stanu wypisywane są na stderr, po jednej linii, w formacie tokenów,
// żeby proces nadrzędny (Python) mógł je łatwo parsować:
//   READY            — strumień wystartował (zgoda na nagrywanie ekranu przyznana)
//   AUDIO            — dotarła pierwsza porcja dźwięku (potwierdzenie, że dane płyną)
//   FRAMES <liczba>  — łączna liczba zapisanych ramek (co ~1 s)
//   STOPPED          — strumień zatrzymany, plik domknięty
//   ERROR <opis>     — błąd krytyczny

import Foundation
import ScreenCaptureKit
import AVFoundation

func log(_ s: String) {
    FileHandle.standardError.write((s + "\n").data(using: .utf8)!)
}

@available(macOS 13.0, *)
final class SystemAudioRecorder: NSObject, SCStreamOutput, SCStreamDelegate {
    private let outputURL: URL
    private var stream: SCStream?
    private var audioFile: AVAudioFile?
    private var sawAudio = false
    private var totalFrames: AVAudioFramePosition = 0
    private var lastReport: AVAudioFramePosition = 0
    private let sampleQueue = DispatchQueue(label: "notetaker.sysaudio.samples")

    init(outputURL: URL) {
        self.outputURL = outputURL
    }

    func start() async throws {
        // Pobierz zawartość do udostępnienia — wybieramy pierwszy ekran (nie nagrywamy obrazu,
        // ale SCK wymaga skonfigurowanego filtra z ekranem, żeby przechwytywać audio).
        let content = try await SCShareableContent.excludingDesktopWindows(
            false, onScreenWindowsOnly: false)
        guard let display = content.displays.first else {
            throw NSError(domain: "sysaudio", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "Nie znaleziono ekranu"])
        }
        let filter = SCContentFilter(display: display,
                                     excludingApplications: [],
                                     exceptingWindows: [])

        let config = SCStreamConfiguration()
        config.capturesAudio = true
        config.sampleRate = 48000
        config.channelCount = 2
        config.excludesCurrentProcessAudio = true   // nie nagrywaj własnych dźwięków
        // Obraz musi być skonfigurowany, ale sprowadzamy go do minimum, by nie marnować CPU.
        config.width = 2
        config.height = 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        config.queueDepth = 6

        let stream = SCStream(filter: filter, configuration: config, delegate: self)
        try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: sampleQueue)
        try await stream.startCapture()
        self.stream = stream
        log("READY")
    }

    func stop() async {
        if let stream = stream {
            try? await stream.stopCapture()
        }
        sampleQueue.sync {
            self.audioFile = nil   // domknięcie i zapis nagłówka WAV
        }
        log("FRAMES \(totalFrames)")
        log("STOPPED")
    }

    // MARK: - SCStreamOutput
    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
                of type: SCStreamOutputType) {
        guard type == .audio, sampleBuffer.isValid else { return }

        guard let formatDesc = CMSampleBufferGetFormatDescription(sampleBuffer),
              let asbdPtr = CMAudioFormatDescriptionGetStreamBasicDescription(formatDesc) else {
            return
        }
        var asbd = asbdPtr.pointee
        guard let format = AVAudioFormat(streamDescription: &asbd) else { return }

        // Utwórz plik przy pierwszej porcji, dopasowując format do tego, co dostarcza SCK.
        if audioFile == nil {
            do {
                // Plik na dysku zapisujemy jako przeplatany (interleaved) WAV — WAV nie obsługuje
                // formatu nieprzeplatanego, więc jawnie to ustawiamy, by uniknąć ostrzeżenia.
                // Format przetwarzania w pamięci (commonFormat/interleaved) zostaje taki, jak
                // dostarcza SCK, żeby zapisywany bufor pasował bez konwersji.
                var fileSettings = format.settings
                fileSettings[AVLinearPCMIsNonInterleaved] = false
                audioFile = try AVAudioFile(forWriting: outputURL,
                                            settings: fileSettings,
                                            commonFormat: format.commonFormat,
                                            interleaved: format.isInterleaved)
            } catch {
                log("ERROR Nie udało się utworzyć pliku: \(error.localizedDescription)")
                return
            }
        }
        guard let audioFile = audioFile else { return }

        let numSamples = CMSampleBufferGetNumSamples(sampleBuffer)
        guard numSamples > 0,
              let pcmBuffer = AVAudioPCMBuffer(pcmFormat: format,
                                               frameCapacity: AVAudioFrameCount(numSamples)) else {
            return
        }
        pcmBuffer.frameLength = AVAudioFrameCount(numSamples)

        let status = CMSampleBufferCopyPCMDataIntoAudioBufferList(
            sampleBuffer, at: 0, frameCount: Int32(numSamples),
            into: pcmBuffer.mutableAudioBufferList)
        guard status == noErr else {
            log("ERROR Kopiowanie PCM nie powiodło się: \(status)")
            return
        }

        do {
            try audioFile.write(from: pcmBuffer)
            totalFrames += AVAudioFramePosition(numSamples)
            if !sawAudio {
                sawAudio = true
                log("AUDIO")
            }
            // Raport co ~1 s (48000 ramek).
            if totalFrames - lastReport >= 48000 {
                lastReport = totalFrames
                log("FRAMES \(totalFrames)")
            }
        } catch {
            log("ERROR Zapis nie powiódł się: \(error.localizedDescription)")
        }
    }

    // MARK: - SCStreamDelegate
    func stream(_ stream: SCStream, didStopWithError error: Error) {
        log("ERROR Strumień zatrzymany: \(error.localizedDescription)")
        exit(1)
    }
}

// MARK: - main

guard #available(macOS 13.0, *) else {
    log("ERROR Wymagany macOS 13 lub nowszy")
    exit(2)
}

let args = CommandLine.arguments
guard args.count >= 2 else {
    log("ERROR Użycie: sysaudio <plik-wyjściowy.wav>")
    exit(2)
}
let outputURL = URL(fileURLWithPath: args[1])
let recorder = SystemAudioRecorder(outputURL: outputURL)

// Obsługa sygnałów — czyste zatrzymanie i domknięcie pliku.
signal(SIGINT, SIG_IGN)
signal(SIGTERM, SIG_IGN)
let stopHandler: @Sendable () -> Void = {
    Task {
        await recorder.stop()
        exit(0)
    }
}
let sigintSrc = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
let sigtermSrc = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
sigintSrc.setEventHandler(handler: stopHandler)
sigtermSrc.setEventHandler(handler: stopHandler)
sigintSrc.resume()
sigtermSrc.resume()

Task {
    do {
        try await recorder.start()
    } catch {
        log("ERROR \(error.localizedDescription)")
        exit(1)
    }
}

dispatchMain()
