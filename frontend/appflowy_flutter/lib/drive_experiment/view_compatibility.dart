// SPDX-License-Identifier: AGPL-3.0-only
import 'package:appflowy/drive_experiment/runtime.dart';
import 'package:appflowy_backend/dispatch/dispatch.dart';
import 'package:appflowy_backend/protobuf/flowy-error/errors.pb.dart';
import 'package:appflowy_backend/protobuf/flowy-folder/protobuf.dart';
import 'package:appflowy_result/appflowy_result.dart';

Future<FlowyResult<ViewPB, FlowyError>> createCompatibleDriveView(
  CreateViewPayloadPB payload,
  ViewSectionPB? section,
) async {
  final created = await FolderEventCreateView(payload).send();
  if (!DriveRuntime.ready || section == ViewSectionPB.Private) return created;
  // This native release rejects the old CreateView section field and defaults
  // new views to Private. Its MoveNestedView section enums remain compatible.
  // Explicitly move the new view into the requested Personal/public list.
  return created.fold(
    (view) async {
      final moved = await FolderEventMoveNestedView(
        MoveNestedViewPayloadPB(
          viewId: view.id,
          newParentId: payload.parentViewId,
          fromSection: ViewSectionPB.Private,
          toSection: ViewSectionPB.Public,
        ),
      ).send();
      return moved.fold(
        (_) => FlowyResult<ViewPB, FlowyError>.success(view),
        (error) => FlowyResult<ViewPB, FlowyError>.failure(error),
      );
    },
    (error) async => FlowyResult<ViewPB, FlowyError>.failure(error),
  );
}
