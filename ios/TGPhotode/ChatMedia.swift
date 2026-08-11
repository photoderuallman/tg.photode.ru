@preconcurrency import AVFoundation
import SwiftUI
import UIKit

struct VideoNotePreview: UIViewRepresentable {
    let session: AVCaptureSession
    let mirrorsVideo: Bool

    func makeUIView(context: Context) -> CameraPreviewView {
        let view = CameraPreviewView()
        view.previewLayer.session = session
        view.previewLayer.videoGravity = .resizeAspectFill
        updateConnection(for: view)
        return view
    }

    func updateUIView(_ view: CameraPreviewView, context: Context) {
        view.previewLayer.session = session
        updateConnection(for: view)
    }

    private func updateConnection(for view: CameraPreviewView) {
        guard let connection = view.previewLayer.connection else { return }
        if connection.isVideoRotationAngleSupported(90) {
            connection.videoRotationAngle = 90
        }
        if connection.isVideoMirroringSupported {
            connection.automaticallyAdjustsVideoMirroring = false
            connection.isVideoMirrored = mirrorsVideo
        }
    }
}

final class CameraPreviewView: UIView {
    override class var layerClass: AnyClass {
        AVCaptureVideoPreviewLayer.self
    }

    var previewLayer: AVCaptureVideoPreviewLayer {
        layer as! AVCaptureVideoPreviewLayer
    }
}

final class VideoNoteRecorder: NSObject,
    ObservableObject,
    AVCaptureFileOutputRecordingDelegate,
    @unchecked Sendable
{
    enum State: Equatable {
        case idle
        case preparing
        case recording
        case finishing
    }

    enum RecorderError: LocalizedError {
        case cameraPermission
        case microphonePermission
        case cameraUnavailable
        case microphoneUnavailable
        case configurationFailed
        case recordingFailed

        var errorDescription: String? {
            switch self {
            case .cameraPermission:
                "Camera access is required to record a video message."
            case .microphonePermission:
                "Microphone access is required to record a video message."
            case .cameraUnavailable:
                "No available iPhone camera was found."
            case .microphoneUnavailable:
                "No available iPhone microphone was found."
            case .configurationFailed:
                "The video-message camera could not be configured."
            case .recordingFailed:
                "The video message could not be recorded."
            }
        }
    }

    @Published private(set) var state: State = .idle
    @Published private(set) var cameraPosition: AVCaptureDevice.Position = .front

    let session = AVCaptureSession()

    private let captureQueue = DispatchQueue(
        label: "ru.photode.telegram.video-note-capture",
        qos: .userInitiated
    )
    private let movieOutput = AVCaptureMovieFileOutput()
    private var videoInput: AVCaptureDeviceInput?
    private var finishContinuation: CheckedContinuation<URL?, Error>?
    private var keepFinishedRecording = true
    private var outputURL: URL?

    @MainActor
    func start() async throws {
        guard state == .idle else { return }
        state = .preparing

        guard await Self.requestAccess(for: .video) else {
            state = .idle
            throw RecorderError.cameraPermission
        }
        guard await Self.requestAccess(for: .audio) else {
            state = .idle
            throw RecorderError.microphonePermission
        }

        do {
            try await withCheckedThrowingContinuation { continuation in
                captureQueue.async { [self] in
                    do {
                        try configureSessionIfNeeded()
                        if !session.isRunning {
                            session.startRunning()
                        }
                        try startRecording()
                        continuation.resume()
                    } catch {
                        continuation.resume(throwing: error)
                    }
                }
            }
            if state == .preparing {
                state = .recording
            }
        } catch {
            state = .idle
            throw error
        }
    }

    @MainActor
    func stop(keepingRecording: Bool) async throws -> URL? {
        guard state == .preparing
                || state == .recording
                || state == .finishing
        else {
            return nil
        }
        state = .finishing

        do {
            let result = try await withCheckedThrowingContinuation {
                (continuation: CheckedContinuation<URL?, Error>) in
                captureQueue.async { [self] in
                    keepFinishedRecording = keepingRecording
                    finishContinuation = continuation
                    if movieOutput.isRecording {
                        movieOutput.stopRecording()
                    } else {
                        finishRecording(
                            url: outputURL,
                            error: RecorderError.recordingFailed
                        )
                    }
                }
            }
            state = .idle
            return result
        } catch {
            state = .idle
            throw error
        }
    }

    @MainActor
    func switchCamera() async throws {
        guard state == .preparing || state == .recording else { return }
        let target: AVCaptureDevice.Position = cameraPosition == .front
            ? .back
            : .front

        try await withCheckedThrowingContinuation { continuation in
            captureQueue.async { [self] in
                do {
                    try replaceVideoInput(position: target)
                    continuation.resume()
                } catch {
                    continuation.resume(throwing: error)
                }
            }
        }
        cameraPosition = target
    }

    nonisolated func fileOutput(
        _ output: AVCaptureFileOutput,
        didFinishRecordingTo outputFileURL: URL,
        from connections: [AVCaptureConnection],
        error: Error?
    ) {
        captureQueue.async { [self] in
            finishRecording(url: outputFileURL, error: error)
        }
    }

    private static func requestAccess(
        for mediaType: AVMediaType
    ) async -> Bool {
        switch AVCaptureDevice.authorizationStatus(for: mediaType) {
        case .authorized:
            true
        case .notDetermined:
            await AVCaptureDevice.requestAccess(for: mediaType)
        default:
            false
        }
    }

    private func configureSessionIfNeeded() throws {
        guard session.inputs.isEmpty else { return }

        session.beginConfiguration()
        defer { session.commitConfiguration() }
        session.sessionPreset = .hd1280x720

        try replaceVideoInput(position: .front)

        guard let microphone = AVCaptureDevice.default(for: .audio) else {
            throw RecorderError.microphoneUnavailable
        }
        let microphoneInput = try AVCaptureDeviceInput(device: microphone)
        guard session.canAddInput(microphoneInput) else {
            throw RecorderError.configurationFailed
        }
        session.addInput(microphoneInput)

        guard session.canAddOutput(movieOutput) else {
            throw RecorderError.configurationFailed
        }
        session.addOutput(movieOutput)
        movieOutput.maxRecordedDuration = CMTime(
            seconds: 60,
            preferredTimescale: 600
        )
    }

    private func replaceVideoInput(
        position: AVCaptureDevice.Position
    ) throws {
        guard let camera = AVCaptureDevice.default(
            .builtInWideAngleCamera,
            for: .video,
            position: position
        ) else {
            throw RecorderError.cameraUnavailable
        }
        let replacement = try AVCaptureDeviceInput(device: camera)

        let ownsConfiguration = !session.inputs.isEmpty
        if ownsConfiguration { session.beginConfiguration() }
        defer {
            if ownsConfiguration { session.commitConfiguration() }
        }

        if let videoInput {
            session.removeInput(videoInput)
        }
        guard session.canAddInput(replacement) else {
            if let videoInput, session.canAddInput(videoInput) {
                session.addInput(videoInput)
            }
            throw RecorderError.configurationFailed
        }
        session.addInput(replacement)
        videoInput = replacement

        if let connection = movieOutput.connection(with: .video),
           connection.isVideoRotationAngleSupported(90) {
            connection.videoRotationAngle = 90
        }
    }

    private func startRecording() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("TGPhotodeVideoNotes", isDirectory: true)
        try FileManager.default.createDirectory(
            at: directory,
            withIntermediateDirectories: true
        )

        if let outputURL {
            try? FileManager.default.removeItem(at: outputURL)
        }
        let url = directory
            .appendingPathComponent(UUID().uuidString)
            .appendingPathExtension("mov")
        outputURL = url

        if let connection = movieOutput.connection(with: .video),
           connection.isVideoRotationAngleSupported(90) {
            connection.videoRotationAngle = 90
        }
        movieOutput.startRecording(to: url, recordingDelegate: self)
    }

    private func finishRecording(url: URL?, error: Error?) {
        if session.isRunning {
            session.stopRunning()
        }

        guard let continuation = finishContinuation else {
            if let url { try? FileManager.default.removeItem(at: url) }
            return
        }
        finishContinuation = nil

        let didFinishSuccessfully: Bool
        if let error = error as NSError? {
            didFinishSuccessfully = error.userInfo[
                AVErrorRecordingSuccessfullyFinishedKey
            ] as? Bool ?? false
        } else {
            didFinishSuccessfully = true
        }

        if !didFinishSuccessfully {
            if let url { try? FileManager.default.removeItem(at: url) }
            continuation.resume(throwing: error ?? RecorderError.recordingFailed)
            return
        }

        guard keepFinishedRecording, let url else {
            if let url { try? FileManager.default.removeItem(at: url) }
            continuation.resume(returning: nil)
            return
        }
        continuation.resume(returning: url)
    }
}
