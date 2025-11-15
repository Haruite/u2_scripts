import cv2
import numpy as np
import os
import glob

src_folder = r"C:\Scans"  # folder includes .bmp scans
dst_folder = r"C:\Scans\tmp"  # output .png image folder


# 局部 RGB 方差
def local_rgb_variance(img, ksize=5):
    img_float = img.astype(np.float32)
    var_map = np.zeros(img.shape[:2], dtype=np.float32)
    for c in range(3):
        channel = img_float[:,:,c]
        mean = cv2.blur(channel, (ksize, ksize))
        mean_sq = cv2.blur(channel**2, (ksize, ksize))
        var_map += mean_sq - mean**2
    print("局部RGB方差计算完成")
    return var_map


# 灰度梯度幅值
def gradient_magnitude(gray, ksize=3):
    grad_x = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=ksize)
    grad_y = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=ksize)
    grad = cv2.magnitude(grad_x, grad_y)
    print("梯度幅值计算完成")
    return grad


# 清理 mask（去掉小连通区域）
def clean_mask(mask, min_area_ratio=0.001):
    h, w = mask.shape
    min_area = h * w * min_area_ratio
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    clean = np.zeros_like(mask)
    kept = 0
    for c in contours:
        if cv2.contourArea(c) >= min_area:
            cv2.drawContours(clean, [c], -1, 255, -1)
            kept += 1
    print(f"mask清理完成, 保留 {kept} 个连通区域")
    return clean


# 边缘线检测（用于倾斜角度计算）
def get_outer_contour_edge(mask, thickness=5):
    """
    返回最外轮廓的四边形（顺时针），绝不交叉，绝对稳定。
    """
    h, w = mask.shape
    edge_mask = np.zeros_like(mask)

    # -------- 1. 找最大轮廓 --------
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        # 完全退化，返回整图矩形
        pts = np.array([[0,0], [w-1,0], [w-1,h-1], [0,h-1]], dtype=np.int32)
        cv2.polylines(edge_mask, [pts.reshape(-1,1,2)], True, 255, thickness)
        print("轮廓缺失，返回全图矩形")
        return edge_mask

    c = max(contours, key=cv2.contourArea)

    # -------- 2. 近似为多边形 --------
    peri = cv2.arcLength(c, True)
    approx = cv2.approxPolyDP(c, 0.02 * peri, True)

    # 若恰好为四边形（最理想）
    if len(approx) == 4:
        pts = approx.reshape(4, 2)
    else:
        # -------- 3. 使用最小外接矩形（绝对稳定有 4 点）--------
        rect = cv2.minAreaRect(c)
        pts = cv2.boxPoints(rect)  # 八点情况也能变成 4 点
        pts = np.int32(pts)

    # -------- 4. 将四点排序为（左上、右上、右下、左下）--------
    pts = np.array(pts)
    s = pts.sum(axis=1)
    diff = np.diff(pts, axis=1).reshape(-1)

    ordered = np.zeros((4,2), dtype=np.int32)
    ordered[0] = pts[np.argmin(s)]      # 左上
    ordered[2] = pts[np.argmax(s)]      # 右下
    ordered[1] = pts[np.argmin(diff)]   # 右上
    ordered[3] = pts[np.argmax(diff)]   # 左下

    # -------- 5. 画出轮廓 --------
    cv2.polylines(edge_mask, [ordered.reshape(-1,1,2)], True, 255, thickness)

    print("边缘轮廓生成完成")
    return edge_mask


# 内容检测（用于裁剪）——改进：膨胀保证边缘完整
def detect_content_mask(img, var_thresh=30, grad_thresh=10):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    var_map = local_rgb_variance(img, ksize=5)
    grad_mag = gradient_magnitude(gray, ksize=3)
    binary = np.where((var_map > var_thresh) | (grad_mag > grad_thresh), 255, 0).astype(np.uint8)

    # 闭运算填充内部
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7))
    mask = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel, iterations=2)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)

    # 清理小连通区域
    mask = clean_mask(mask, min_area_ratio=0.001)

    # 膨胀保证边缘完整（防止倾斜拉直时边缘缺失）
    mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5)), iterations=1)

    print(f"内容 mask 生成完成，非零像素: {np.count_nonzero(mask)}")
    return mask


# 计算倾斜角度
def compute_skew_angle_from_edges(mask):
    """
    基于所有边缘点拟合一条直线计算倾斜角度
    mask: 二值边缘 mask
    返回角度，正数为顺时针旋转需要拉直
    """
    ys, xs = np.where(mask > 0)
    if len(xs) < 2:
        print("倾斜角计算点不足，返回0")
        return 0.0

    # 拟合直线 y = k*x + b
    vx, vy, x0, y0 = cv2.fitLine(np.column_stack((xs, ys)), cv2.DIST_L2, 0, 0.01, 0.01)
    vx = float(vx[0])
    vy = float(vy[0])
    angle = np.degrees(np.arctan2(vy, vx))
    print(f"倾斜角度拟合完成: {angle:.2f}°")
    return angle


# 旋转图像保持全部内容
def rotate_keep_all(img, angle):
    h, w = img.shape[:2]
    center = (w//2, h//2)
    m = cv2.getRotationMatrix2D(center, angle, 1.0)
    cos = abs(m[0,0])
    sin = abs(m[0,1])
    new_w = int(h*sin + w*cos)
    new_h = int(h*cos + w*sin)
    m[0,2] += new_w/2 - center[0]
    m[1,2] += new_h/2 - center[1]
    rotated = cv2.warpAffine(img, m, (new_w, new_h), borderValue=(255,255,255))
    print(f"图像旋转完成: 新尺寸 {rotated.shape}")
    return rotated


# 根据边缘裁剪内容
def crop_content_by_edges(img, mask, margin=10):
    ys, xs = np.where(mask > 0)
    if len(xs) == 0:
        print("裁剪失败：mask为空")
        return img
    top = np.min(ys)
    bottom = np.max(ys)
    left = np.min(xs)
    right = np.max(xs)
    h, w = img.shape[:2]
    x1 = max(0, left - margin)
    y1 = max(0, top - margin)
    x2 = min(w, right + margin)
    y2 = min(h, bottom + margin)
    print(f"裁剪框: x1={x1}, y1={y1}, x2={x2}, y2={y2}")
    return img[y1:y2, x1:x2]


# 单图处理
def process_image(path, out_path):
    img = cv2.imread(path)
    if img is None:
        print(f"无法读取: {path}")
        return
    print(f"处理：{path}")

    # 1) 边缘线 mask -> 计算倾斜角
    mask = detect_content_mask(img)
    edge_mask = get_outer_contour_edge(mask)

    angle = compute_skew_angle_from_edges(edge_mask)

    # 2) 旋转图像
    rotated = rotate_keep_all(img, angle)

    # 3) 内容 mask -> 裁剪
    content_mask = detect_content_mask(rotated)
    final = crop_content_by_edges(rotated, content_mask)

    # 保存
    cv2.imwrite(out_path, final)
    print("已保存 ->", out_path)


# 批量处理文件夹
def batch_process_folder(in_folder, out_folder):
    os.makedirs(out_folder, exist_ok=True)
    files = glob.glob(os.path.join(in_folder, "*.bmp"))
    print(f"找到 {len(files)} 张bmp文件")
    for f in files:
        name = os.path.splitext(os.path.basename(f))[0]
        out_path = os.path.join(out_folder, name + ".png")
        process_image(f, out_path)


if __name__ == "__main__":
    batch_process_folder(src_folder, dst_folder)
