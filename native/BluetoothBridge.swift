// BLE transport only. Pairing, encryption, and audit decisions live in Python.
import Foundation
import CoreBluetooth

let rxID = CBUUID(string: "690c9e36-68e1-4367-aad4-98b4e78d0001")
let txID = CBUUID(string: "690c9e36-68e1-4367-aad4-98b4e78d0002")
let maxPacket = 16384

func emit(_ value: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: value) else { return }
    FileHandle.standardOutput.write(data + Data([10]))
}

final class Bridge: NSObject, CBPeripheralManagerDelegate {
    var manager: CBPeripheralManager!
    let serviceID: CBUUID
    var tx: CBMutableCharacteristic!
    var central: CBCentral?
    var input = Data()
    var output = Data()
    var lastActivity = Date()
    var watchdog: Timer?

    init(service: String) {
        serviceID = CBUUID(string: service)
        super.init()
        manager = CBPeripheralManager(delegate: self, queue: .main,
            options: [CBPeripheralManagerOptionShowPowerAlertKey: true])
        watchdog = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            guard let self = self, self.central != nil else { return }
            if Date().timeIntervalSince(self.lastActivity) > 30 { self.reset() }
        }
    }

    func reset() {
        central = nil
        input.removeAll()
        output.removeAll()
        emit(["event": "disconnected"])
    }

    func peripheralManagerDidUpdateState(_ peripheral: CBPeripheralManager) {
        reset()
        if peripheral.state == .poweredOn {
            peripheral.removeAllServices()
            tx = CBMutableCharacteristic(type: txID, properties: [.notify], value: nil, permissions: [])
            let rx = CBMutableCharacteristic(type: rxID, properties: [.write], value: nil,
                                             permissions: [.writeable])
            let service = CBMutableService(type: serviceID, primary: true)
            service.characteristics = [rx, tx]
            peripheral.add(service)
        } else {
            emit(["event": "state", "message": peripheral.state == .poweredOff
                  ? "Turn on Bluetooth on the Mac."
                  : "Bluetooth unavailable. Check Bluetooth permission in System Settings."])
        }
    }

    func peripheralManager(_ peripheral: CBPeripheralManager, didAdd service: CBService, error: Error?) {
        guard error == nil else { emit(["event": "error", "message": "Could not create Bluetooth service."]); return }
        // Advertise only one UUID so it fits the primary advertisement Android can see.
        // Apple's overflow UUID area is not discoverable by Android.
        peripheral.startAdvertising([CBAdvertisementDataServiceUUIDsKey: [serviceID]])
    }

    func peripheralManagerDidStartAdvertising(_ peripheral: CBPeripheralManager, error: Error?) {
        emit(["event": error == nil ? "ready" : "error",
              "message": error == nil ? "Ready to pair" : "Could not advertise Bluetooth service."])
    }

    func peripheralManager(_ peripheral: CBPeripheralManager, central: CBCentral,
                           didSubscribeTo characteristic: CBCharacteristic) {
        if self.central != nil && self.central?.identifier != central.identifier { return }
        reset()
        self.central = central
        lastActivity = Date()
        emit(["event": "connected"])
    }

    func peripheralManager(_ peripheral: CBPeripheralManager, central: CBCentral,
                           didUnsubscribeFrom characteristic: CBCharacteristic) {
        if self.central?.identifier == central.identifier { reset() }
    }

    func peripheralManager(_ peripheral: CBPeripheralManager, didReceiveWrite requests: [CBATTRequest]) {
        guard let first = requests.first else { return }
        for request in requests {
            guard request.characteristic.uuid == rxID, request.offset == 0,
                  request.central.identifier == central?.identifier, let bytes = request.value,
                  input.count + bytes.count <= maxPacket else {
                peripheral.respond(to: first, withResult: .unlikelyError)
                input.removeAll()
                return
            }
            lastActivity = Date()
            input.append(bytes)
            while let end = input.firstIndex(of: 10) {
                let packet = Data(input[..<end])
                input.removeSubrange(...end)
                emit(["event": "packet", "data": packet.base64EncodedString()])
            }
        }
        peripheral.respond(to: first, withResult: .success)
    }

    func receive(_ command: [String: Any]) {
        if command["op"] as? String == "reset" { reset(); return }
        guard command["op"] as? String == "send", central != nil,
              let text = command["data"] as? String, let bytes = Data(base64Encoded: text),
              output.count + bytes.count + 1 <= maxPacket * 2 else { return }
        output.append(bytes)
        output.append(10)
        pump()
    }

    func pump() {
        guard let central = central, let tx = tx else { return }
        while !output.isEmpty {
            let length = min(output.count, central.maximumUpdateValueLength)
            guard length > 0 else { return }
            let chunk = Data(output.prefix(length))
            if !manager.updateValue(chunk, for: tx, onSubscribedCentrals: [central]) { return }
            output.removeFirst(length)
        }
    }

    func peripheralManagerIsReady(toUpdateSubscribers peripheral: CBPeripheralManager) { pump() }
}

guard CommandLine.arguments.count == 2, UUID(uuidString: CommandLine.arguments[1]) != nil else { exit(2) }
let bridge = Bridge(service: CommandLine.arguments[1])
DispatchQueue.global(qos: .userInitiated).async {
    while let line = readLine() {
        guard line.utf8.count <= maxPacket * 2, let bytes = line.data(using: .utf8),
              let value = try? JSONSerialization.jsonObject(with: bytes) as? [String: Any] else { continue }
        DispatchQueue.main.async { bridge.receive(value) }
    }
    exit(0)
}
RunLoop.main.run()
