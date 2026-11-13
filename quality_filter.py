import os
def main():
    total_checked = 0
    total_qualified = 0
    total_review = 0
    total_rejected = 0

    batch_size = int(os.getenv("QUALITY_BATCH_SIZE", "100"))

    print("开始检查全部待筛选网站")
    print(f"每批处理数量：{batch_size}")

    while True:
        websites = get_pending_quality_websites(batch_size)

        if not websites:
            print("没有更多待筛选网站")
            break

        batch_checked = 0

        for website in websites:
            try:
                result = evaluate_website(website)

                save_quality_result(
                    website["domain"],
                    result
                )

                batch_checked += 1
                total_checked += 1

                if result["quality_status"] == "qualified":
                    total_qualified += 1
                elif result["quality_status"] == "review":
                    total_review += 1
                else:
                    total_rejected += 1

            except Exception as e:
                print(
                    f"[ERROR] {website.get('domain')}: {e}"
                )

        print(
            f"[BATCH] 本批处理 {batch_checked}/{len(websites)}，"
            f"累计检查 {total_checked}，"
            f"合格 {total_qualified}，"
            f"待复核 {total_review}，"
            f"不合格 {total_rejected}"
        )

        # 如果整批都没有成功处理，避免无限循环
        if batch_checked == 0:
            print("[STOP] 本批没有成功处理的网站，停止运行")
            break

    print("\n========== 筛选完成 ==========")
    print(f"总检查：{total_checked}")
    print(f"合格：{total_qualified}")
    print(f"待复核：{total_review}")
    print(f"不合格：{total_rejected}")


if __name__ == "__main__":
    main()