const inputFile_img = document.querySelector('#file_img');
const imgArea_img = document.querySelector('.img-area_img');
const selectImageBtn_img = document.querySelector('.select-image_img');

// Make the imgArea_img clickable to open file dialog
imgArea_img.addEventListener('click', function () {
	inputFile_img.click();
});

// Also allow the "Select Image" button to trigger the file input
selectImageBtn_img.addEventListener('click', function () {
	inputFile_img.click();
});

inputFile_img.addEventListener('change', function () {
	const image_img = this.files[0];
	if (image_img && image_img.size < 2000000) { // 2MB limit check
		const reader_img = new FileReader();
		reader_img.onload = () => {
			// Remove any previously displayed image
			const allImg_img = imgArea_img.querySelectorAll('img');
			allImg_img.forEach(item => item.remove());

			// Display the selected image in imgArea_img
			const imgUrl_img = reader_img.result;
			const img_img = document.createElement('img');
			img_img.src = imgUrl_img;
			imgArea_img.appendChild(img_img);
			imgArea_img.classList.add('active_img');
			imgArea_img.dataset.img = image_img.name;

			// Enable the "Select Image" button (if needed, for future use)
			selectImageBtn_img.disabled = false;
		};
		reader_img.readAsDataURL(image_img);
	} else {
		alert("Image size is more than 2MB");
	}
});
